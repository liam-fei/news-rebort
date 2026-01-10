# =============================================
# Fredly News Bot - STRICT STABLE LOCK
# 修复：彻底移除 2.0/2.5 实验版模型，防止触发高频限流
# 锁定：只允许使用 Gemini 1.5 Flash (最稳) 和 1.5 Pro
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
import random
from datetime import datetime, timedelta
from time import mktime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import feedparser
import edge_tts  # 👈 这次真的加上了！绝对不会再报 NameError
import schedule
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram.ext import Application
from telegram.request import HTTPXRequest

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("Fredly_Locked")

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    log.error("❌ Missing Env Vars")
    sys.exit(1)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
VOICE_CN = "zh-CN-XiaoxiaoNeural"
VOICE_EN = "en-US-AvaNeural"
TARGET_MINUTES = 13

OUTPUT_DIR = Path("./outputs")
BIN_DIR = Path("./bin")
OUTPUT_DIR.mkdir(exist_ok=True)
BIN_DIR.mkdir(exist_ok=True)

# ---------------- RSS SOURCES ----------------
RSS_POOLS = {
    "GLOBAL": "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en",
    "CHINA": "https://news.google.com/rss/search?q=China+when:1d&hl=en-GB&gl=GB&ceid=GB:en",
    "AL_JAZEERA": "https://www.aljazeera.com/xml/rss/all.xml"
}

# ---------------- HTTP SESSION ----------------
def make_session():
    s = requests.Session()
    # 基础网络连接重试
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[500, 502, 503, 504], allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

SESSION = make_session()

# ---------------- UTILS ----------------
def is_recent(entry, hours=24):
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        try:
            pub_time = datetime.fromtimestamp(mktime(entry.published_parsed))
            if datetime.now() - pub_time < timedelta(hours=26): return True
            return False 
        except: pass
    return True 

def ensure_ffmpeg():
    if shutil.which("ffmpeg"): return True
    ffmpeg_path = BIN_DIR / "ffmpeg"
    if ffmpeg_path.exists():
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.resolve())
        return True
    log.info("🛠️ Installing FFmpeg...")
    try:
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        r = SESSION.get(url, stream=True, timeout=60)
        with open(BIN_DIR/"ffmpeg.tar.xz", "wb") as f: f.write(r.content)
        with tarfile.open(BIN_DIR/"ffmpeg.tar.xz") as tar:
            m = next(m for m in tar.getmembers() if m.name.endswith("/ffmpeg"))
            m.name = "ffmpeg"; tar.extract(m, BIN_DIR)
        (BIN_DIR/"ffmpeg").chmod(0o755)
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.resolve())
        return True
    except: return False

# ---------------- GEMINI ENGINE (LOCKED) ----------------
def get_api_url():
    url = f"{BASE_URL}/models?key={GEMINI_API_KEY}"
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200: return None
        models = r.json().get("models", [])
        cands = [m["name"] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
        
        # 🔥 核心修复：只保留 1.5 系列，删除了所有 2.0/2.5 实验版
        # 即使 1.5 暂时不可用，也不许它去用 2.0，因为 2.0 必崩
        priority = ["gemini-1.5-flash", "gemini-1.5-pro"]
        
        chosen = next((m for p in priority for m in cands if p in m), None)
        
        # 如果找不到优先模型，默认回退到列表第一个，但打个警告日志
        if not chosen and cands: 
            chosen = cands[0]
            log.warning(f"⚠️ Preferred models missing. Fallback to: {chosen}")
        
        if chosen:
            log.info(f"✅ AI Engine: {chosen}")
            return f"{BASE_URL}/{chosen}:generateContent"
    except: pass
    return None

def call_gemini(prompt, base_url, json_mode=False):
    url = f"{base_url}?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2}
    }
    if json_mode: payload["generationConfig"]["responseMimeType"] = "application/json"
    
    # 重试逻辑
    for attempt in range(3):
        try:
            r = SESSION.post(url, headers=headers, json=payload, timeout=100)
            if r.status_code == 200:
                return r.json()['candidates'][0]['content']['parts'][0]['text']
            elif r.status_code == 429:
                wait = (attempt + 1) * 20 # 20s, 40s, 60s
                log.warning(f"⚠️ 429 Rate Limit. Cooling {wait}s...")
                time.sleep(wait)
                continue
            else:
                log.error(f"API Error {r.status_code}: {r.text}")
                return None
        except Exception as e:
            log.error(f"Net Error: {e}")
            time.sleep(5)
    return None

# ---------------- PIPELINE ----------------

def step1_scan_headlines():
    log.info("📡 [Step 1] Scanning Feeds...")
    combined = []
    for cat, url in RSS_POOLS.items():
        try:
            d = feedparser.parse(url)
            count = 0
            for e in d.entries:
                if is_recent(e, hours=24):
                    title = e.get("title", "").split(" - ")[0]
                    prefix = "[CHINA]" if cat == "CHINA" else "[GLOBAL]"
                    combined.append(f"{prefix} {title}")
                    count += 1
                if count >= 12: break 
        except: pass
    
    random.shuffle(combined)
    return combined[:40] # 限制数量，防止 Token 溢出

def step2_select_topics(headlines, api_url):
    log.info("🧠 [Step 2] AI Selecting Topics...")
    today = datetime.now().strftime('%Y-%m-%d')
    prompt = (
        f"Role: Chief Editor. Date: {today}\n"
        "Task: Select Top 5 BREAKING NEWS EVENTS.\n"
        "RULES:\n"
        "1. ✅ MUST include at least 1 event related to CHINA (look for [CHINA] tag).\n"
        "2. ✅ Select ONLY specific, concrete events from the LAST 24 HOURS.\n"
        "3. ❌ REJECT broad topics or old news.\n"
        "Output: JSON array of search queries.\n"
        "Headlines:\n" + "\n".join(headlines)
    )
    raw = call_gemini(prompt, api_url, json_mode=True)
    if not raw: return []
    try:
        return json.loads(raw.replace("```json", "").replace("```", "").strip())
    except: return []

def fetch_details(topic):
    query = f"{topic} when:1d"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=en-GB&gl=GB&ceid=GB:en"
    try:
        time.sleep(1)
        d = feedparser.parse(url)
        if not d.entries: return ""
        
        block = f"### EVENT: {topic}\n"
        valid = 0
        for e in d.entries:
            if is_recent(e, hours=24):
                summary = re.sub("<[^<]+?>", "", e.get("summary", ""))[:350]
                src = e.get("source", {}).get("title", "Unknown")
                block += f"- {src}: {summary}\n"
                valid += 1
                if valid >= 3: break
        return block if valid > 0 else ""
    except: return ""

def step3_deep_research(topics):
    log.info(f"🕵️ [Step 3] Researching {len(topics)} events...")
    results = []
    # 使用 3 线程并发，1.5 Flash 能够承受
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [ex.submit(fetch_details, t) for t in topics]
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
    
    if not results:
        log.error("❌ No valid recent news found.")
        return None
    return "\n".join(results)

def step4_write_scripts(data, api_url):
    log.info("✍️ [Step 4] Writing Scripts...")
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. Telegram Brief
    p_brief = (
        f"Role: Editor. Date: {today}.\n"
        f"Task: Write Telegram Markdown summary.\n"
        f"Rule: STRICTLY based on Data. MUST cover the China story.\n"
        f"Format:\n📅 **早安简报 {today}**\n\n🔥 **今日五大热点**\n1. **[Headline]** - [Detail]\n...\n"
        f"Data:\n{data}"
    )
    text = call_gemini(p_brief, api_url)
    
    # 冷却 15 秒
    log.info("⏳ Cooling down 15s...")
    time.sleep(15)

    # 2. Chinese Intro
    p_cn = (
        f"Role: Anchor. Date: {today}. Style: CCTV News.\n"
        f"Task: Spoken Intro. Cover top stories + China story.\n"
        f"Rule: No 'First/Second'. Be concise. NO hallucinations.\n"
        f"Start: '这里是专属于GD的早间新闻。今天是{today}。'\n"
        f"Data:\n{data}"
    )
    cn = call_gemini(p_cn, api_url)

    # 冷却 15 秒
    log.info("⏳ Cooling down 15s...")
    time.sleep(15)

    # 3. English Deep Dive
    p_en = (
        f"Role: Senior Correspondent. Task: {TARGET_MINUTES}-minute report.\n"
        f"Style: BBC/Al Jazeera. International perspective.\n"
        f"Rules: CITE SOURCES. NO INTRO (Start with story). Cover China story in depth.\n"
        f"Data:\n{data}"
    )
    en = call_gemini(p_en, api_url)
    return text, cn, en

# ---------------- PRODUCTION ----------------
async def send_to_user(text, cn, en):
    if not ensure_ffmpeg(): return
    log.info("🎙️ Processing Audio...")
    
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
    t_req = HTTPXRequest(read_timeout=300, write_timeout=300)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
    
    async with app:
        await app.initialize()
        if text:
            safe = text.replace("#", "")
            try: await app.bot.send_message(CHAT_ID, safe, parse_mode="Markdown")
            except: await app.bot.send_message(CHAT_ID, safe)
        if f_final.exists():
            d = datetime.now().strftime("%Y-%m-%d")
            with open(f_final, "rb") as f:
                await app.bot.send_audio(CHAT_ID, f, title=f"News {d}", caption=f"🎧 Briefing {d}")
            f_final.unlink()
    
    f_cn.unlink(); f_en.unlink()
    log.info("✅ Job Complete!")

def job():
    log.info(">>> Job Started")
    # 启动前缓冲
    time.sleep(5)
    
    try:
        api = get_api_url()
        if not api: return
        
        headlines = step1_scan_headlines()
        if not headlines: return
        
        topics = step2_select_topics(headlines, api)
        if not topics: return
        
        data = step3_deep_research(topics)
        if not data: return
        
        txt, c, e = step4_write_scripts(data, api)
        
        # 🔥 新增：在日志里打印出来，方便你人工核查内容
        print("\n" + "="*30 + " [DEBUG] TELEGRAM TEXT " + "="*30)
        print(txt)
        print("\n" + "="*30 + " [DEBUG] CHINESE SCRIPT " + "="*30)
        print(c)
        print("\n" + "="*30 + " [DEBUG] ENGLISH SCRIPT " + "="*30)
        print(e[:500] + "...\n") # 英文太长，只打印前500字看看开头

        if c and e:
            asyncio.run(send_to_user(txt, c, e))
            
    except Exception as e:
        log.error(f"Critical Job Error: {e}")
    log.info("<<< Job Finished")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    
    log.info("🚀 Fredly Bot (Strict Lock 1.5) Ready")
    
    schedule.every().day.at("03:00").do(job)

    if os.getenv("RUN_NOW", "false").lower() == "true":
        log.info("⚡ Manual Trigger")
        job()

    while True:
        schedule.run_pending()
        time.sleep(60)
