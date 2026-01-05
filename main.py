# =============================================
# Fredly News Bot - Final Production (v1 Fix)
# =============================================

import os
import sys
import asyncio
import schedule
import time
import feedparser
import edge_tts
from datetime import datetime
from pathlib import Path
from google import genai
from telegram.ext import Application
from telegram.request import HTTPXRequest

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("❌ 环境变量缺失，请检查 Render 设置")
    sys.exit(1)

# ---------------- CLIENT (v1 FIXED) ----------------
# 使用你发现的 v1 锁死方案，解决 404 问题
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={"api_version": "v1"} 
)

MODEL_ID = "gemini-1.5-flash"  # 既然 v1 通了，建议用 1.5-flash，效果远好于 1.0
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
    print("\n📡 抓取实时新闻...")
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
                print(f"⚠️ 跳过源 {url}: {e}")
    print(f"✅ 抓取完成，共 {len(articles)} 篇")
    return articles

def generate_podcast_script(articles):
    print(f"🤖 Gemini ({MODEL_ID}) 撰写脚本中...")
    prompt = (
        f"You are Sara, a warm news anchor. Create a {TARGET_MINUTES}-minute news script. "
        f"Professional, spoken style. Plain text only. Articles: \n"
    )
    for art in articles:
        prompt += f"[{art['category']}] {art['title']}: {art['summary']}\n---\n"

    try:
        response = client.models.generate_content(model=MODEL_ID, contents=prompt)
        if response.text:
            print("✅ 脚本生成成功")
            return response.text
    except Exception as e:
        print(f"❌ Gemini 失败: {e}")
        return None

async def tts_and_upload(script_text):
    date_str = datetime.now().strftime("%Y-%m-%d")
    mp3_path = OUTPUT_DIR / f"news_{date_str}.mp3"

    print("🎙️ 语音合成中 (Edge TTS)...")
    try:
        await edge_tts.Communicate(script_text, VOICE_NAME).save(mp3_path)
    except Exception as e:
        print(f"❌ TTS 失败: {e}"); return

    print("📤 上传 Telegram...")
    try:
        # 增加上传超时，防止大文件失败
        t_request = HTTPXRequest(read_timeout=300.0, write_timeout=300.0)
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()
        async with app:
            await app.initialize()
            with open(mp3_path, "rb") as f:
                await app.bot.send_audio(chat_id=CHAT_ID, audio=f, caption=f"🎙️ News Briefing {date_str}")
        print("✅ 发送成功")
        mp3_path.unlink(missing_ok=True)
    except Exception as e:
        print(f"❌ Telegram 失败: {e}")

# ---------------- SCHEDULER ----------------

def run_daily_job():
    print(f"\n>>> 任务启动: {datetime.now()}")
    news = fetch_rss_news()
    if not news: return
    script = generate_podcast_script(news)
    if not script: return
    asyncio.run(tts_and_upload(script))
    print("<<< 任务结束\n")

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()

    print(f"🚀 Fredly News Bot (v1 Fix) 已就绪")
    
    # 每天 03:00 UTC (迪拜 07:00) 运行
    schedule.every().day.at("03:00").do(run_daily_job)

    # 如果需要立即运行测试
    if os.getenv("RUN_NOW", "false").lower() == "true":
        run_daily_job()

    while True:
        schedule.run_pending()
        time.sleep(60)
