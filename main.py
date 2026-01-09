# =============================================
# Fredly News Bot - Logic Fixed (Specific Events)
# 修复：强制 AI 选具体新闻事件，拒绝空洞的宏观话题
# 模式：默认开启验证模式 (不发TG)，满意后可切换为定时任务
# =============================================

import os
import sys
import time
import json
import tarfile
import asyncio
import shutil
import re
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("Fredly_Fixed")

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not GEMINI_API_KEY:
    log.error("❌ Missing GEMINI_API_KEY")
    sys.exit(1)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
VOICE_CN = "zh-CN-XiaoxiaoNeural"
VOICE_EN = "en-US-AvaNeural"
TARGET_MINUTES = 13

OUTPUT_DIR = Path("./outputs")
BIN_DIR = Path("./bin")
OUTPUT_DIR.mkdir(exist_ok=True)
BIN_DIR.mkdir(exist_ok=True)

# ---------------- HTTP SESSION ----------------
def make_session():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

SESSION = make_session()

# ---------------- FFmpeg ----------------
def ensure_ffmpeg():
    if shutil.which("ffmpeg"):
        return True
    
    ffmpeg_path = BIN_DIR / "ffmpeg"
    if ffmpeg_path.exists():
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.resolve())
        return True

    log.info("🛠️ Installing FFmpeg...")
    url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
    tar_path = BIN_DIR / "ffmpeg.tar.xz"

    try:
        r = SESSION.get(url, stream=True, timeout=60)
        r.raise_for_status()
        with open(tar_path, "wb") as f:
            for c in r.iter_content(8192): f.write(c)
        with tarfile.open(tar_path, "r:xz") as tar:
            member = next((m for m in tar.getmembers() if m.name.endswith("/ffmpeg")), None)
            if not member: raise RuntimeError("No ffmpeg bin")
            member.name = "ffmpeg"
            tar.extract(member, path=BIN_DIR)
        
        ffmpeg_path.chmod(0o755)
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.resolve())
        return True
    except Exception as e:
        log.error(f"FFmpeg install error: {e}")
        return False
    finally:
        if tar_path.exists(): tar_path.unlink()

# ---------------- GEMINI ENGINE ----------------
def get_api_url():
    # 使用 URL 参数鉴权
    url = f"{BASE_URL}/models?key={GEMINI_API_KEY}"
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200:
            log.error(f"❌ API Error: {r.status_code}")
            return None
            
        models = r.json().get("models", [])
        cands = [m["name"] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
        priority = ["gemini-2.0-pro", "gemini-1.5-pro", "gemini-2.5", "gemini-2.0-flash", "gemini-1.5-flash"]
        chosen = next((m for p in priority for m in cands if p in m), cands[0] if cands else None)
            
        if chosen:
            log.info(f"✅ Logic Engine: {chosen}")
            return f"{BASE_URL}/{chosen}:generateContent"
    except Exception as e:
        log.error(f"Model discovery failed: {e}")
    return None

def call_gemini(prompt, base_url, json_mode=False):
    url = f"{base_url}?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2} 
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    try:
        r = SESSION.post(url, headers=headers, json=payload, timeout=100)
        if r.status_code != 200:
            log.error(f"Gemini Error: {r.text}")
            return None
        return r.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        log.error(f"Gemini Net Error: {e}")
        return None

# ---------------- PIPELINE STEPS ----------------

def step1_scan_headlines():
    log.info("📡 [Step 1] Scanning Headlines...")
    url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    try:
        d = feedparser.parse(url)
        return [e.get("title", "").split(" - ")[0] for e in d.entries[:50]]
    except: return []

def step2_select_topics(headlines, api_url):
    log.info("🧠 [Step 2] AI Editor Selecting Top 5 SPECIFIC EVENTS...")
    # 🔥 核心修复：强制要求具体事件，禁止宏观类别
    prompt = (
        "Role: Chief Editor.\n"
        "Task: Identify the TOP 5 specific breaking news EVENTS from the headlines.\n"
        "CRITICAL INSTRUCTION: Return specific search queries for concrete events, NOT broad categories.\n"
        "❌ BAD: 'International Trade', 'Space Technology', 'Public Health', 'US Politics'\n"
        "✅ GOOD: 'Red Sea shipping attacks', 'SpaceX Starship launch failure', 'WHO declares new pandemic', 'Senate passes border bill'\n"
        "Output: A strictly JSON array of strings.\n"
        "Headlines Pool:\n" + "\n".join(headlines)
    )
    raw = call_gemini(prompt, api_url, json_mode=True)
    if not raw: return []
    
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        topics = json.loads(clean)
        log.info(f"🔹 Selected Events: {topics}")
        return topics
    except Exception as e:
        log.error(f"JSON error: {e}")
        return []

def fetch_topic_details(topic):
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(topic)}&hl=en-US&gl=US&ceid=US:en"
    try:
        d = feedparser.parse(url)
        block = f"### EVENT: {topic}\n"
        for e in d.entries[:3]:
            summary = re.sub("<[^<]+?>", "", e.get("summary", ""))[:400]
            src = e.get("source", {}).get("title", "Unknown")
            block += f"- Source ({src}): {summary}\n"
        return block
    except: return f"### EVENT: {topic} (Fetch Failed)\n"

def step3_deep_research(topics):
    log.info(f"🕵️ [Step 3] Deep Researching {len(topics)} events...")
    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(fetch_topic_details, t) for t in topics]
        for f in as_completed(futures):
            results.append(f.result())
    return "\n".join(results)

def step4_write_scripts(research_data, api_url):
    log.info("✍️ [Step 4] Writing Scripts...")
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 简报
    p_brief = (
        f"Role: Editor. Date: {today}.\n"
        f"Task: Write a Telegram Markdown summary.\n"
        f"Format:\n📅 **早安简报 {today}**\n\n🔥 **今日五大热点**\n1. **[Specific Headline]** - [Key detail]\n...\n"
        f"Data:\n{research_data}"
    )
    text = call_gemini(p_brief, api_url)

    # 2. 中文导语
    p_cn = (
        f"Role: Professional Anchor. Date: {today}.\n"
        f"Task: Spoken Chinese Intro. Style: CCTV News/Formal.\n"
        f"Requirements: Be concrete. Mention specific numbers/names/places from the data.\n"
        f"Start: '这里是Fredly早间新闻。今天是{today}。'\n"
        f"End: '以下是详细英文报道。'\n"
        f"Data:\n{research_data}"
    )
    cn = call_gemini(p_cn, api_url)

    # 3. 英文正文
    p_en = (
        f"Role: Senior Correspondent. Task: {TARGET_MINUTES}-minute deep report.\n"
        f"Style: BBC/Reuters. Formal, Objective.\n"
        f"Structure:\n"
        f"1. NO INTRO. Start directly with the biggest story.\n"
        f"2. Cover all 5 events. Cite specific sources (e.g. 'According to CNN...').\n"
        f"3. Smooth transitions.\n"
        f"Length: ~1600 words.\n"
        f"Data:\n{research_data}"
    )
    en = call_gemini(p_en, api_url)
    return text, cn, en

# ---------------- PRODUCTION UTILS ----------------
async def produce_audio_and_send(text, cn, en):
    if not ensure_ffmpeg(): return
    log.info("🎙️ Producing Audio...")
    
    import telegram
    from telegram.ext import Application
    
    f_cn = OUTPUT_DIR / "cn.mp3"
    f_en = OUTPUT_DIR / "en.mp3"
    f_final = OUTPUT_DIR / "final.mp3"
    
    await edge_tts.Communicate(cn, VOICE_CN).save(f_cn)
    await edge_tts.Communicate(en, VOICE_EN).save(f_en)
    
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(f_cn), "-i", str(f_en), 
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a];[a]volume=1.3[out]", 
         "-map", "[out]", str(f_final)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    
    log.info("📤 Sending to Telegram...")
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    async with app:
        await app.initialize()
        if text:
            safe = text.replace("#", "")
            try: await app.bot.send_message(CHAT_ID, safe, parse_mode="Markdown")
            except: await app.bot.send_message(CHAT_ID, safe)
        if f_final.exists():
            today = datetime.now().strftime("%Y-%m-%d")
            with open(f_final, "rb") as f:
                await app.bot.send_audio(CHAT_ID, f, title=f"News {today}", caption=f"🎧 Briefing {today}")
            f_final.unlink()
    f_cn.unlink(); f_en.unlink()
    log.info("✅ Done!")

# ---------------- RUNNERS ----------------

def run_verification():
    """验证模式：只打印，不生成音频"""
    log.info("🧪 STARTING VERIFICATION (Dry Run)")
    api_url = get_api_url()
    if not api_url: return

    headlines = step1_scan_headlines()
    if not headlines: return

    topics = step2_select_topics(headlines, api_url)
    if not topics: return

    research = step3_deep_research(topics)
    print(f"\n📝 RESEARCH SAMPLE:\n{research[:500]}...\n")

    text, cn, en = step4_write_scripts(research, api_url)
    
    print("\n" + "="*30 + " TELEGRAM BRIEF " + "="*30 + "\n" + text)
    print("\n" + "="*30 + " CHINESE INTRO " + "="*30 + "\n" + cn)
    print("\n" + "="*30 + " ENGLISH SCRIPT " + "="*30 + "\n" + en[:1000] + "...\n")
    log.info("✅ Verification Complete")

def job():
    """生产模式：全流程"""
    log.info(">>> Job Started")
    api_url = get_api_url()
    if not api_url: return
    
    headlines = step1_scan_headlines()
    if not headlines: return
    
    topics = step2_select_topics(headlines, api_url)
    if not topics: return
    
    research = step3_deep_research(topics)
    text, cn, en = step4_write_scripts(research, api_url)
    
    if cn and en:
        asyncio.run(produce_audio_and_send(text, cn, en))
    log.info("<<< Job Finished")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    # 👇 1. 想要验证内容？保持这样直接运行
    run_verification()

    # 👇 2. 想要正式部署？注释掉上面一行，取消下面注释
    # from keep_alive import keep_alive
    # keep_alive()
    # import schedule
    # log.info("🚀 Fredly News Bot Ready (Schedule Mode)")
    # schedule.every().day.at("03:00").do(job)
    # while True:
    #     schedule.run_pending()
    #     time.sleep(60)
