# =============================================
# Fredly News Bot - Smart Discovery Edition
# 智能探测模型 (优先 2.0/2.5 -> 后备 1.5)
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

# 使用 v1beta 接口以获取最新的实验模型列表
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

VOICE_NAME = "en-US-AvaNeural"
TARGET_MINUTES = 15
ARTICLES_LIMIT = 3

RSS_FEEDS = {
    "Global News": ["http://feeds.bbci.co.uk/news/rss.xml", "http://rss.cnn.com/rss/edition.rss"],
    "Business": ["https://feeds.bloomberg.com/markets/news.rss"],
    "Tech": ["https://techcrunch.com/feed/"],
    "Sports": ["https://www.espn.com/espn/rss/news"]
}

OUTPUT_DIR = Path("./outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

# ---------------- SMART MODEL FINDER ----------------

def get_working_api_url():
    """
    自动向 Google 询问可用模型，并返回可用的生成 URL。
    """
    print("🔍 Auto-detecting available models...")
    try:
        # 1. 获取模型列表
        list_url = f"{BASE_URL}/models?key={GEMINI_API_KEY}"
        resp = requests.get(list_url, timeout=10)
        
        if resp.status_code != 200:
            print(f"❌ Failed to get models list: {resp.text}")
            return None

        data = resp.json()
        if 'models' not in data:
            print(f"❌ API Key valid but no models found. (Check Google AI Studio)")
            return None

        # 2. 筛选出支持文本生成的模型
        candidates = []
        print("📋 Available Models for your Key:")
        for m in data['models']:
            if 'generateContent' in m.get('supportedGenerationMethods', []):
                model_name = m['name']
                candidates.append(model_name)
                # 打印出来方便调试
                print(f"   -> {model_name}")

        if not candidates:
            print("❌ No text generation models available.")
            return None

        # 3. 智能选择：优先 2.x Flash/Pro -> 1.5 Flash -> 其他
        # 注意：Google 返回的 name 通常包含 'models/' 前缀
        priority_patterns = [
            'gemini-2.5',       # 未来版本
            'gemini-2.0-flash', # 极速版
            'gemini-2.0-pro',   # 强力版
            'gemini-1.5-flash', # 最稳后备
            'gemini-1.5-pro',   # 强力后备
        ]
        
        chosen_model = None
        for pattern in priority_patterns:
            # 在候选列表中寻找包含该 pattern 的模型
            match = next((m for m in candidates if pattern in m), None)
            if match:
                chosen_model = match
                print(f"⚡ Match found for priority '{pattern}': {chosen_model}")
                break
        
        if not chosen_model:
            chosen_model = candidates[0]  # 兜底用第一个
            print(f"⚠️ No priority match, using fallback: {chosen_model}")
        
        print(f"✅ Selected working model: {chosen_model}")
        
        # 4. 构造最终 URL
        # chosen_model 已经包含 'models/' 前缀，直接拼接
        generate_url = f"{BASE_URL}/{chosen_model}:generateContent?key={GEMINI_API_KEY}"
        return generate_url

    except Exception as e:
        print(f"❌ Discovery failed: {e}")
        return None

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
    # 动态获取 URL
    api_url = get_working_api_url()
    if not api_url:
        print("❌ Could not find a valid model URL. Aborting.")
        return None

    print(f"🤖 Generating Script...")
    
    prompt_text = (
        f"You are Sara, a warm news anchor. Create a {TARGET_MINUTES}-minute news script. "
        f"Professional, spoken style. Plain text only. Articles: \n"
    )
    for art in articles:
        prompt_text += f"[{art['category']}] {art['title']}: {art['summary']}\n---\n"

    payload = {
        "contents": [{
            "parts": [{"text": prompt_text}]
        }]
    }

    try:
        response = requests.post(
            api_url, 
            headers={'Content-Type': 'application/json'},
            data=json.dumps(payload),
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            try:
                if 'candidates' in result and result['candidates']:
                    script = result['candidates'][0]['content']['parts'][0]['text']
                    print("✅ Script Generated Successfully")
                    return script
                else:
                    print(f"❌ Empty Response (Safety/Quota?): {result}")
                    return None
            except (KeyError, IndexError):
                print(f"❌ Json Parse Error: {result}")
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

    print(f"🚀 Fredly News Bot (Smart Discovery) Ready")
    schedule.every().day.at("03:00").do(run_daily_job)

    if os.getenv("RUN_NOW", "false").lower() == "true":
        run_daily_job()

    while True:
        schedule.run_pending()
        time.sleep(60)
