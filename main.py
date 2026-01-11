# =============================================
# Fredly News Bot - SYNTAX FIX EDITION
# 修复：使用三引号 (""") 重写所有 Prompt，彻底防止字符串换行报错
# 策略：Golden Balance (叙事感 + 事实性 + 体育)
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
import schedule
import edge_tts 
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
log = logging.getLogger("Fredly_Fix")

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
    "BUSINESS": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWvfSkdnUVZNREZVTm5WNU5FbENkM1JmY2dFUEAV?hl=en-GB&gl=GB&ceid=GB:en",
    "POLITICS": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWvfSkdnUVZNREZVTm5WNU5FbENkM1JmY2dFUEAV?hl=en-GB&gl=GB&ceid=GB:en",
    "CHINA": "https://news.google.com/rss/search?q=China+when:1d&hl=en-GB&gl=GB&ceid=GB:en",
    "SPORTS": "https://news.google.com/rss/search?q=NBA+OR+Soccer+OR+Sports+when:1d&hl=en-GB&gl=GB&ceid=GB:en"
}

# ---------------- HTTP SESSION ----------------
def make_session():
    s = requests.Session()
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

# ---------------- GEMINI ENGINE ----------------
def get_api_url():
    url = f"{BASE_URL}/models?key={GEMINI_API_KEY}"
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200: return None
        models = r.json().get("models", [])
        cands = [m["name"] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
        priority = ["gemini-1.5-flash", "gemini-2.5-flash", "gemini-1.5-pro"]
        chosen = next((m for p in priority for m in cands if p in m), None)
        if not chosen and cands: chosen = cands[0]
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
        "generationConfig": {"temperature": 0.3} # 0.3: 既严谨又流畅
    }
    if json_mode: payload["generationConfig"]["responseMimeType"] = "application/json"
    
    for attempt in range(3):
        try:
            r = SESSION.post(url, headers=headers, json=payload, timeout=100)
            if r.status_code == 200:
                return r.json()['candidates'][0]['content']['parts'][0]['text']
            elif r.status_code == 429:
                wait = (attempt + 1) * 20
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
                    prefix = f"[{cat}]"
                    combined.append(f"{prefix} {title}")
                    count += 1
                if count >= 15: break 
        except: pass
    random.shuffle(combined)
    return combined[:60]

def step2_select_topics(headlines, api_url):
    log.info("🧠 [Step 2] AI Selecting Topics...")
    today = datetime.now().strftime('%Y-%m-%d')
    
    # 🔥 修复重点：使用三引号 (""") 包裹 Prompt，防止 SyntaxError
    prompt_base = f"""Role: Chief Editor. Date: {today}
Task: Select Top 5 HEADLINES.
SELECTION CRITERIA:
1. ✅ MUST include 1 event related to CHINA ([CHINA] tag).
2. ✅ MUST include 1 MAJOR SPORTS event (NBA/Soccer).
3. ✅ The other 3 must be HIGH IMPACT Geopolitics/Economy events.
4. ❌ IGNORE: Small local accidents, celebrity gossip.
Output: JSON array of search queries.
Headlines:
"""
    # 拼接标题列表
    prompt = prompt_base + "\n".join(headlines)

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
    log.info("✍️ [Step 4] Writing Scripts (Safe & Fluent)...")
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. Telegram Brief
    p_brief = f"""Role: Senior Analyst. Date: {today}.
Task: Write a High-Impact Summary.
Format:
📅 **早安简报 {today}**

🔥 **今日重点**
1. **[Headline]** - [Brief Context/Why it matters]
...
Data:
{data}"""
    
    text = call_gemini(p_brief, api_url)
    log.info("⏳ Cooling down 30s...")
    time.sleep(30)

    # 2. Chinese Intro
    p_cn = f"""Role: Senior News Anchor (like Bai Yansong/CCTV). Date: {today}.
Style: Professional, Authoritative, but Engaging (Not robotic).
Task: Deliver the morning news to GD.
INSTRUCTIONS:
1. CONNECT THE DOTS: Don't just list facts. Explain the context logically based on the data.
2. TONE: Confident and steady. Use professional terminology for Biz/Politics.
3. SPORTS: End with the sports news in a slightly more energetic tone.
4. START: '这里是专属于GD的早间新闻。今天是{today}。'
Data:
{data}"""

    cn = call_gemini(p_cn, api_url)
    log.info("⏳ Cooling down 30s...")
    time.sleep(30)

    # 3. English Deep Dive
    p_en = f"""Role: NPR/BBC Senior Correspondent. Task: {TARGET_MINUTES}-minute report.
Style: Narrative Journalism. Intelligent, Flowing, Insightful.
INSTRUCTIONS:
1. Don't just read headlines. Weave the stories together into a coherent narrative.
2. Focus on the 'WHY' and 'HOW' based on the provided facts.
3. CITE SOURCES naturally (e.g., 'As reported by Reuters...').
4. Start directly with the most significant story.
Data:
{data}"""

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
        
        # 调试打印
        if txt: print("\n=== TELEGRAM ===\n" + txt)
        if c: print("\n=== CHINESE ===\n" + c)
        if e: print("\n=== ENGLISH ===\n" + e[:200] + "...")

        if c and e:
            asyncio.run(send_to_user(txt, c, e))
        else:
            log.error("❌ Scripts incomplete.")
            
    except Exception as e:
        log.error(f"Critical Job Error: {e}")
    log.info("<<< Job Finished")

# ---------------- MAIN ----------------
if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    
    log.info("🚀 Fredly Bot (Syntax Fix) Ready")
    
    schedule.every().day.at("03:00").do(job)

    if os.getenv("RUN_NOW", "false").lower() == "true":
        log.info("⚡ Manual Trigger")
        job()

    while True:
        schedule.run_pending()
        time.sleep(60)
