# =============================================
# Fredly Daily News - Gemini + EdgeTTS (Free)
# =============================================

import feedparser
import google.generativeai as genai
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
# 必须从环境变量获取
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# 检查环境变量是否存在
if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("❌ 错误: 缺少必要的环境变量 (GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID)")
    sys.exit(1)

# 配置 Gemini
genai.configure(api_key=GEMINI_API_KEY)

# 语音配置 (推荐: en-US-AvaNeural, en-US-EmmaNeural, en-GB-SoniaNeural)
VOICE_NAME = "en-US-AvaNeural" 

# 新闻源
RSS_FEEDS = {
    'Global News': ['http://feeds.bbci.co.uk/news/rss.xml', 'http://rss.cnn.com/rss/edition.rss'],
    'Business': ['https://feeds.bloomberg.com/markets/news.rss', 'https://www.cnbc.com/id/100003114/device/rss/rss.html'],
    'Tech': ['https://techcrunch.com/feed/', 'https://www.wired.com/feed/rss'],
    'Entertainment': ['https://variety.com/feed/'],
    'Sports': ['https://www.espn.com/espn/rss/news']
}

OUTPUT_DIR = Path('./outputs')
OUTPUT_DIR.mkdir(exist_ok=True)

TARGET_MINUTES = 20  # 目标时长
ARTICLES_LIMIT = 4   # 每个分类抓取几篇

# Telegram 上传配置 (大文件需要更长的超时时间)
t_request = HTTPXRequest(connection_pool_size=8, read_timeout=300.0, write_timeout=300.0, connect_timeout=60.0)

# ---------------- HELPERS ----------------

def fetch_latest_articles():
    """从 RSS 获取新闻"""
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
                    # 简单清洗 HTML 标签 (如果需要更强清洗可用 BeautifulSoup，但 Gemini 也能读懂 HTML)
                    all_articles.append({
                        'category': category,
                        'title': entry.get('title', ''),
                        'summary': summary[:1000] # 截取摘要防止过长
                    })
                    count += 1
            except Exception as e:
                print(f'  ⚠️ 跳过源 {feed_url}: {e}')
                continue
    print(f'✅ 共抓取 {len(all_articles)} 篇文章')
    return all_articles

def generate_script_with_gemini(articles):
    """Gemini 编写脚本"""
    print("🤖 Gemini 正在撰写新闻稿...")
    
    prompt = f"""
    Role: You are Sara, a professional, warm, and engaging news anchor.
    Date: {datetime.now().strftime('%B %d, %Y')}
    Task: Create a cohesive {TARGET_MINUTES}-minute daily news podcast script.
    
    Instructions:
    1. **Structure**: Intro -> Politics/Global -> Business -> Tech -> Entertainment -> Sports -> Outro.
    2. **Style**: Conversational but professional. Use smooth transitions (e.g., "Turning to the markets...", "In the tech world...").
    3. **Content**: Synthesize the provided articles. Don't just list them. Connect the dots.
    4. **Formatting**: Plain text only. NO markdown (no **bold**, no # headers). This text will be read by a machine directly.
    5. **Length**: Approximately 2500-3000 words.

    Source Articles:
    """
    for art in articles:
        prompt += f"\nSection: {art['category']}\nHeadline: {art['title']}\nSummary: {art['summary']}\n---"

    model = genai.GenerativeModel('gemini-1.5-flash')
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        print(f"❌ Gemini API Error: {e}")
        return None

async def process_audio_and_send(script_text):
    """异步生成音频并发送"""
    date_str = datetime.now().strftime('%Y-%m-%d')
    mp3_path = OUTPUT_DIR / f'briefing_{date_str}.mp3'

    # 1. 生成音频
    print(f"🎙️ 正在合成语音 ({VOICE_NAME})...")
    try:
        communicate = edge_tts.Communicate(script_text, VOICE_NAME)
        await communicate.save(mp3_path)
        print(f"✅ 音频已保存: {mp3_path}")
    except Exception as e:
        print(f"❌ TTS Error: {e}")
        return

    # 2. 发送 Telegram
    print("📤 正在上传至 Telegram (这可能需要一分钟)...")
    try:
        # 每次发送时独立构建 Application，确保连接新鲜
        app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_request).build()
        async with app:
            await app.initialize()
            with open(mp3_path, 'rb') as audio_file:
                await app.bot.send_audio(
                    chat_id=CHAT_ID, 
                    audio=audio_file, 
                    caption=f'🎙️ Daily Briefing - {date_str}',
                    title=f"News {date_str}",
                    performer="Sara (Gemini AI)"
                )
        print("✅ 发送成功！")
        
        # 发送完成后删除文件节省空间
        if os.path.exists(mp3_path):
            os.remove(mp3_path)
            
    except Exception as e:
        print(f"❌ Telegram Error: {e}")

# ---------------- SCHEDULER WRAPPER ----------------

def job():
    """Schedule 调用的同步入口"""
    print(f'\n>>> 任务开始: {datetime.now()}')
    
    # 1. 抓取
    articles = fetch_latest_articles()
    if not articles:
        print("❌ 未获取到文章，任务终止")
        return

    # 2. 写稿
    script = generate_script_with_gemini(articles)
    if not script: return

    # 3. 异步处理音频和发送
    asyncio.run(process_audio_and_send(script))
    
    print(f'<<< 任务结束: {datetime.now()}\n')

# ---------------- ENTRY POINT ----------------

if __name__ == "__main__":
    # 启动 Flask 保活服务
    from keep_alive import keep_alive
    keep_alive()

    print(f"\n🚀 Fredly News Bot (Gemini Edition) 已启动")
    print(f"⏰ 定时任务设定: 每天迪拜时间 07:00 (UTC 03:00)")
    
    # 设定定时任务 (Render 默认是 UTC 时间)
    # 迪拜是 UTC+4，所以迪拜 07:00 = UTC 03:00
    schedule.every().day.at("03:00").do(job)

    # 测试开关：如果环境变量 RUN_NOW=true，启动时立即运行一次
    if os.getenv("RUN_NOW", "false").lower() == "true":
        print("🔥 检测到测试指令，立即运行一次...")
        job()

    while True:
        schedule.run_pending()
        time.sleep(60)
