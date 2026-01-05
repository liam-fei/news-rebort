# =============================================
# Fredly News Bot - Gemini 1.0 Pro Edition
# =============================================

import os
import sys
import asyncio
import schedule
import time
import feedparser
import edge_tts
import requests
import json
from datetime import datetime
from pathlib import Path
from telegram.ext import Application
from telegram.request import HTTPXRequest

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("Error: Missing Environment Variables")
    sys.exit(1)

# 🔑 核心修改：强制使用 1.0 Pro
GEMINI_MODEL = "gemini-1.0-pro"

# 使用 v1 稳定版接口，不再使用 v1beta
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"

VOICE_NAME = "en-US-AvaNeural"
TARGET_MINUTES = 15
ARTICLES_LIMIT = 3

RSS_FEEDS = {
    "Global News": ["http://feeds.bbci.co.uk/news/rss.xml", "http://rss.cnn.com/rss/edition.rss"],
    "Business": ["https://feeds.bloomberg.com/markets/news.rss", "https://www.cnbc.com/id/100003114/device/rss/rss.html"],
    "Tech": ["https://techcrunch.com/feed/", "https://www.wired.com/feed/rss"],
    "Entertainment": ["https://variety.com/feed/"],
    "Sports": ["https://www.espn.com/espn/rss/news"]
}

OUTPUT_DIR = Path("./outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------------- CORE LOGIC ----------------

def fetch_rss_news():
    print("\n>>> Fetching RSS Feeds...")
    articles = []
    for category, feeds in RSS_FEEDS.items():
        count = 0
        for url in feeds:
            if count >= ARTICLES_LIMIT: break
            try:
                d = feedparser.parse(url)
                for entry in d.entries[:ARTICLES_LIMIT]:
                    articles.append({
                        "category": category,
                        "title": entry.get("title", ""),
                        "summary": entry.get("summary", "")[:1000]
                    })
                    count += 1
            except Exception as e:
                print(f"Skip feed {url}: {e}")
    print(f"✅ Fetched {len(articles)} articles")
    return articles

def generate_script_via_http(articles):
    print(f"🤖 Generating Script via HTTP ({GEMINI_MODEL})...")
    
    prompt_text = (
        f"You are Sara, a warm news anchor. Create a {TARGET_MINUTES}-minute news script. "
        f"Professional, spoken style. Plain text only. Articles: \n"
    )
    for art in articles:
        prompt_text += f"[{art['category']}] {art['title']}: {art['summary']}\n---\n"

    # 1.0 Pro 的 API 结构非常标准
    payload = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }]
    }

    try:
        response = requests.post(
            GEMINI_URL, 
            headers={'Content-Type': 'application/json'},
            data=json.dumps(payload),
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            try:
                # 尝试解析返回结果
                if 'candidates' in result and result['candidates']:
                    script = result['candidates'][0]['content']['parts'][0]['text']
                    print("✅ Script Generated Successfully")
                    return script
                else:
                    print(f"❌ Gemini blocked the response (Safety/Other): {result}")
                    return None
            except (KeyError, IndexError):
                print(f"❌ Parse Error: {result}")
                return None
        else:
            print(f"❌ API Error: {response.status_code} - {response.text}")
            return None

    except Exception as e:
        print(f"❌ Connection Error: {e}")
        return None

async def tts_and_upload(script_text):
    date_str = datetime.now().strftime("%Y-%m-%d")
    mp3_path = OUTPUT_DIR / f"news_{date_str}.mp3"

    print("🎙️ Synthesizing Audio (Edge TTS)...")
    try:
        await edge_tts.Communicate(script_text, VOICE_NAME).save(mp3_path)
    except Exception as e:
        print(f"❌ TTS Failed: {e}"); return

    print("📤 Uploading to Telegram...")
    try:
        # 设置更长的超时时间
        t_request = HTTPXRequest(read_timeout=300.0, write_timeout=300.0)
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()
        async with app:
            await app.initialize()
            with open(mp3_path, "rb") as f:
                await app.bot.send_audio(chat_id=CHAT_ID, audio=f, caption=f"🎙️ News Briefing {date_str}")
        print("✅ Message Sent!")
        if mp3_path.exists():
            os.remove(mp3_path)
    except Exception as e:
        print(f"❌ Telegram Failed: {e}")

# ---------------- SCHEDULER ----------------

def run_daily_job():
    print(f"\n>>> Job Started: {datetime.now()}")
    news = fetch_rss_news()
    if not news: return
    script = generate_script_via_http(news)
    if not script: return
    asyncio.run(tts_and_upload(script))
    print("<<< Job Finished\n")

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()

    print(f"🚀 Fredly News Bot (Gemini 1.0) Ready")
    
    # 每天 UTC 03:00 (迪拜 07:00)
    schedule.every().day.at("03:00").do(run_daily_job)

    if os.getenv("RUN_NOW", "false").lower() == "true":
        run_daily_job()

    while True:
        schedule.run_pending()
        time.sleep(60)
