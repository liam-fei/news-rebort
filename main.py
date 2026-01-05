# =============================================
# Fredly News Bot - Truly Stable 0 Cost Edition
# Gemini (Safe Fallback) + Edge TTS
# =============================================

import feedparser
from google import genai
from telegram.ext import Application
from telegram.request import HTTPXRequest
import schedule
import time
from pathlib import Path
from datetime import datetime
import asyncio
import os
import edge_tts
import sys

# ---------------- CONFIG ----------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("❌ 环境变量未设置完整")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

VOICE_NAME = "en-US-AvaNeural"
TARGET_MINUTES = 15
ARTICLES_LIMIT = 3

RSS_FEEDS = {
    "Global News": [
        "http://feeds.bbci.co.uk/news/rss.xml",
        "http://rss.cnn.com/rss/edition.rss",
    ],
    "Business": [
        "https://feeds.bloomberg.com/markets/news.rss",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    ],
    "Tech": [
        "https://techcrunch.com/feed/",
        "https://www.wired.com/feed/rss",
    ],
    "Entertainment": ["https://variety.com/feed/"],
    "Sports": ["https://www.espn.com/espn/rss/news"],
}

OUTPUT_DIR = Path("./outputs")
OUTPUT_DIR.mkdir(exist_ok=True)

t_request = HTTPXRequest(
    connection_pool_size=8,
    read_timeout=300.0,
    write_timeout=300.0,
    connect_timeout=60.0,
)

# ---------------- MODEL STRATEGY ----------------
# 原则：永远有一个“保底模型”，flash 只是加速选项

SAFE_MODEL = "models/gemini-1.5-pro"
FLASH_KEYWORD = "flash"

_cached_models = None


def get_preferred_model():
    global _cached_models
    if _cached_models:
        return _cached_models

    print("🔍 探测 Gemini 可用模型...")
    try:
        models = client.models.list()
        usable = [
            m.name
            for m in models
            if "generateContent" in getattr(m, "supported_generation_methods", [])
        ]

        if not usable:
            print("❌ 未发现任何可用模型，使用保底模型")
            _cached_models = SAFE_MODEL
            return SAFE_MODEL

        # 如果 flash 存在，只作为优先项
        for m in usable:
            if FLASH_KEYWORD in m:
                print(f"⚡ 使用 Flash 模型: {m}")
                _cached_models = m
                return m

        print(f"🛡️ 使用保底模型: {usable[0]}")
        _cached_models = usable[0]
        return usable[0]

    except Exception as e:
        print(f"⚠️ 模型探测失败，回退保底模型: {e}")
        _cached_models = SAFE_MODEL
        return SAFE_MODEL


# ---------------- RSS ----------------

def fetch_latest_articles():
    print("\n📡 抓取新闻源...")
    articles = []

    for category, feeds in RSS_FEEDS.items():
        count = 0
        for feed_url in feeds:
            if count >= ARTICLES_LIMIT:
                break
            try:
                d = feedparser.parse(feed_url)
                for entry in d.entries:
                    if count >= ARTICLES_LIMIT:
                        break
                    articles.append(
                        {
                            "category": category,
                            "title": entry.get("title", ""),
                            "summary": entry.get("summary", "")[:1000],
                        }
                    )
                    count += 1
            except Exception as e:
                print(f"⚠️ 跳过源 {feed_url}: {e}")

    print(f"✅ 抓取 {len(articles)} 篇文章")
    return articles


# ---------------- GEMINI ----------------

def generate_script_with_gemini(articles):
    print("🤖 Gemini 正在生成新闻稿...")

    model_id = get_preferred_model()
    print(f"🎯 使用模型: {model_id}")

    prompt = (
        f"You are Sara, a professional news anchor.\n"
        f"Create a natural {TARGET_MINUTES}-minute spoken news script.\n"
        f"Plain text only. No markdown.\n\n"
    )

    for art in articles:
        prompt += (
            f"[{art['category']}]\n"
            f"{art['title']}\n"
            f"{art['summary']}\n\n"
        )

    try:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"❌ Gemini 调用失败: {e}")
        return None


# ---------------- TTS + TELEGRAM ----------------

async def process_audio_and_send(script_text):
    date_str = datetime.now().strftime("%Y-%m-%d")
    mp3_path = OUTPUT_DIR / f"briefing_{date_str}.mp3"

    print("🎙️ 合成语音中...")
    try:
        await edge_tts.Communicate(script_text, VOICE_NAME).save(mp3_path)
    except Exception as e:
        print(f"❌ TTS 失败: {e}")
        return

    print("📤 发送至 Telegram...")
    try:
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()
        async with app:
            await app.initialize()
            with open(mp3_path, "rb") as f:
                await app.bot.send_audio(
                    chat_id=CHAT_ID,
                    audio=f,
                    caption=f"🎙️ Fredly Daily Briefing - {date_str}",
                )
        mp3_path.unlink(missing_ok=True)
        print("✅ 发送完成")
    except Exception as e:
        print(f"❌ Telegram 错误: {e}")


# ---------------- JOB ----------------

def job():
    print(f"\n>>> 任务开始: {datetime.now()}")
    articles = fetch_latest_articles()
    if not articles:
        return

    script = generate_script_with_gemini(articles)
    if not script:
        return

    asyncio.run(process_audio_and_send(script))
    print("<<< 任务结束\n")


# ---------------- MAIN ----------------

if __name__ == "__main__":
    from keep_alive import keep_alive

    keep_alive()
    print("\n🚀 Fredly News Bot 已启动")

    schedule.every().day.at("03:00").do(job)

    if os.getenv("RUN_NOW", "false").lower() == "true":
        job()

    while True:
        schedule.run_pending()
        time.sleep(60)
