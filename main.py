# =============================================
# Fredly News Bot - Gemini Robust Version
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
    print("❌ 错误: 缺少必要的环境变量")
    sys.exit(1)

# 初始化新版客户端
client = genai.Client(api_key=GEMINI_API_KEY)
VOICE_NAME = "en-US-AvaNeural" 

RSS_FEEDS = {
    'Global News': ['http://feeds.bbci.co.uk/news/rss.xml', 'http://rss.cnn.com/rss/edition.rss'],
    'Business': ['https://feeds.bloomberg.com/markets/news.rss', 'https://www.cnbc.com/id/100003114/device/rss/rss.html'],
    'Tech': ['https://techcrunch.com/feed/', 'https://www.wired.com/feed/rss'],
    'Entertainment': ['https://variety.com/feed/'],
    'Sports': ['https://www.espn.com/espn/rss/news']
}

OUTPUT_DIR = Path('./outputs')
OUTPUT_DIR.mkdir(exist_ok=True)
TARGET_MINUTES = 15
ARTICLES_LIMIT = 3

t_request = HTTPXRequest(connection_pool_size=8, read_timeout=300.0, write_timeout=300.0, connect_timeout=60.0)

# ---------------- HELPERS ----------------

def fetch_latest_articles():
    print(f'\n📡 正在抓取 RSS 新闻源...')
    all_articles = []
    for category, feeds in RSS_FEEDS.items():
        count = 0
        for feed_url in feeds:
            if count >= ARTICLES_LIMIT: break
            try:
                d = feedparser.parse(feed_url)
                for entry in d.entries:
                    if count >= ARTICLES_LIMIT: break
                    summary = entry.get('summary', entry.get('description', ''))
                    all_articles.append({
                        'category': category,
                        'title': entry.get('title', ''),
                        'summary': summary[:1000]
                    })
                    count += 1
            except Exception as e:
                print(f'  ⚠️ 跳过源 {feed_url}: {e}')
                continue
    print(f'✅ 共抓取 {len(all_articles)} 篇文章')
    return all_articles

def generate_script_with_gemini(articles):
    print("🤖 Gemini 正在尝试撰写新闻稿...")
    
    prompt = f"Role: You are Sara, a professional news anchor. Create a {TARGET_MINUTES}-minute news script. Articles: "
    for art in articles:
        prompt += f"\n[{art['category']}] {art['title']}: {art['summary']}\n---"

    # 依次尝试这些可能的模型名称
    models_to_try = ['gemini-1.5-flash', 'gemini-1.5-flash-8b', 'gemini-1.5-pro']
    
    for model_id in models_to_try:
        try:
            print(f"🔄 尝试模型: {model_id}...")
            response = client.models.generate_content(
                model=model_id,
                contents=prompt
            )
            print(f"✅ 使用模型 {model_id} 成功生成脚本")
            return response.text
        except Exception as e:
            if "404" in str(e):
                print(f"⚠️ 模型 {model_id} 不可用 (404)，尝试下一个...")
                continue
            else:
                print(f"❌ 调用 {model_id} 时发生错误: {e}")
                return None
    
    print("❌ 所有模型均无法调用。请检查 Google AI Studio 的 API Key 权限。")
    return None

async def process_audio_and_send(script_text):
    date_str = datetime.now().strftime('%Y-%m-%d')
    mp3_path = OUTPUT_DIR / f'briefing_{date_str}.mp3'

    print(f"🎙️ 正在合成语音 (15分钟可能需要一点时间)...")
    try:
        communicate = edge_tts.Communicate(script_text, VOICE_NAME)
        await communicate.save(mp3_path)
        print(f"✅ 音频合成完毕")
    except Exception as e:
        print(f"❌ TTS 错误: {e}")
        return

    print("📤 正在发送至 Telegram...")
    try:
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()
        async with app:
            await app.initialize()
            with open(mp3_path, 'rb') as audio_file:
                await app.bot.send_audio(
                    chat_id=CHAT_ID, 
                    audio=audio_file, 
                    caption=f'🎙️ Daily Briefing - {date_str}'
                )
        print("✅ Telegram 发送成功！")
        if os.path.exists(mp3_path): os.remove(mp3_path)
    except Exception as e:
        print(f"❌ Telegram 发送错误: {e}")

def job():
    print(f'\n>>> 任务开始: {datetime.now()}')
    articles = fetch_latest_articles()
    if not articles: return
    script = generate_script_with_gemini(articles)
    if not script: return
    asyncio.run(process_audio_and_send(script))
    print(f'<<< 任务结束\n')

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()

    print(f"\n🚀 Fredly News Bot 已准备就绪")
    schedule.every().day.at("03:00").do(job)

    if os.getenv("RUN_NOW", "false").lower() == "true":
        job()

    while True:
        schedule.run_pending()
        time.sleep(60)
