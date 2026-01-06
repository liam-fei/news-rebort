# =============================================
# Fredly News Bot - Final Complete Edition
# 特性：文字简报(Markdown) + 语音播报 + 自动防休眠接口
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
import tarfile
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from telegram.ext import Application
from telegram.request import HTTPXRequest
from telegram.constants import ParseMode # 用于发送 Markdown 格式文字

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("❌ Error: Missing Environment Variables")
    sys.exit(1)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
VOICE_CN = "zh-CN-XiaoxiaoNeural"
VOICE_EN = "en-US-AvaNeural"
TARGET_MINUTES = 12
CANDIDATE_POOL_SIZE = 40 

RSS_FEEDS = {
    "Global": ["https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"],
    "Tech": ["https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en"],
    "Business": ["https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"]
}

OUTPUT_DIR = Path("./outputs")
OUTPUT_DIR.mkdir(exist_ok=True)
BIN_DIR = Path("./bin")
BIN_DIR.mkdir(exist_ok=True)

# ---------------- 0. FFmpeg Setup ----------------
def ensure_ffmpeg():
    ffmpeg_path = BIN_DIR / "ffmpeg"
    if ffmpeg_path.exists():
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.absolute())
        return True
    print("🛠️ Installing FFmpeg...")
    try:
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        r = requests.get(url, stream=True)
        t_path = BIN_DIR / "ff.tar.xz"
        with open(t_path, "wb") as f:
            for c in r.iter_content(8192): f.write(c)
        with tarfile.open(t_path, "r:xz") as tar:
            for m in tar.getmembers():
                if m.name.endswith("/ffmpeg"):
                    m.name = "ffmpeg"
                    tar.extract(m, path=BIN_DIR)
                    break
        (BIN_DIR/"ffmpeg").chmod(0o755)
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.absolute())
        t_path.unlink()
        return True
    except: return False

# ---------------- 1. API ----------------
def get_api_url():
    try:
        r = requests.get(f"{BASE_URL}/models?key={GEMINI_API_KEY}", timeout=10)
        if r.status_code!=200: return None
        cands = [m['name'] for m in r.json().get('models',[]) if 'generateContent' in m.get('supportedGenerationMethods',[])]
        prio = ['gemini-2.5', 'gemini-2.0-flash', 'gemini-1.5-pro', 'gemini-1.5-flash']
        chosen = next((m for p in prio for m in cands if p in m), cands[0] if cands else None)
        if chosen: return f"{BASE_URL}/{chosen}:generateContent?key={GEMINI_API_KEY}"
    except: pass
    return None

def call_gemini(prompt, url):
    try:
        r = requests.post(url, headers={'Content-Type':'application/json'}, data=json.dumps({"contents":[{"parts":[{"text":prompt}]}]}), timeout=90)
        if r.status_code==200: return r.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e: print(f"Gemini Err: {e}")
    return None

# ---------------- 2. FETCH & GEN ----------------
def fetch_rss_news():
    print("\n📡 Fetching RSS...")
    articles = []
    seen = set()
    for cat, feeds in RSS_FEEDS.items():
        for url in feeds:
            if len(articles) >= CANDIDATE_POOL_SIZE: break
            try:
                d = feedparser.parse(url)
                for entry in d.entries[:10]: 
                    title = entry.get("title", "").split(" - ")[0]
                    if title and title not in seen:
                        articles.append(f"[{cat}] {title}")
                        seen.add(title)
            except: pass
    print(f"✅ Collected {len(articles)} headlines.")
    return articles

def generate_content(articles):
    url = get_api_url()
    if not url: return None, None, None
    news_text = "\n".join(articles)
    today_str = datetime.now().strftime("%Y-%m-%d")

    print("🤖 Generating Content...")

    # [1] 文字简报 (Telegram Markdown)
    # 专门用于发送文字消息，使用 Emoji 和列表，方便阅读
    p_text_brief = (
        f"Role: News Editor. Context: Morning Briefing {today_str}.\n"
        f"Task: Select the Top 5 most important stories from the list.\n"
        f"Output: A clean Markdown summary in Chinese.\n"
        f"Format:\n"
        f"📅 **早安简报 {today_str}**\n\n"
        f"🌍 **全球头条**\n- [Story 1 headline]\n- [Story 2 headline]\n\n"
        f"💻 **科技财经**\n- [Story 3 headline]\n- [Story 4 headline]\n\n"
        f"👇 *详细深度分析请收听下方音频*\n"
        f"Headlines: {news_text}"
    )
    text_brief = call_gemini(p_text_brief, url)

    # [2] 中文导语 (语音稿) - 央视风
    p_cn_audio = (
        f"Role: News Anchor. Context: {today_str}.\n"
        f"Task: Spoken Chinese Intro. Select top 4 stories.\n"
        f"Style: CCTV News. Formal. No 'First/Second'.\n"
        f"Start: '这里是Fredly早间新闻。今天是{today_str}。'\n"
        f"End: '以下是详细英文报道。'\n"
        f"Headlines: {news_text}"
    )
    cn_audio = call_gemini(p_cn_audio, url)

    # [3] 英文正文 (语音稿) - CNN风
    p_en_audio = (
        f"Role: Senior Correspondent.\n"
        f"Task: {TARGET_MINUTES}-minute deep dive report.\n"
        f"Style: BBC/CNN. Formal. NO GREETING (Start with story).\n"
        f"Content: 3 Deep Dives + 5 Briefs.\n"
        f"Length: ~1600 words.\n"
        f"Headlines: {news_text}"
    )
    en_audio = call_gemini(p_en_audio, url)

    return text_brief, cn_audio, en_audio

# ---------------- 3. PRODUCTION ----------------
async def produce_audio(cn_txt, en_txt):
    if not ensure_ffmpeg(): return None
    print("🎙️ Audio Production...")
    
    f_cn = OUTPUT_DIR / "part1.mp3"
    f_en = OUTPUT_DIR / "part2.mp3"
    f_final = OUTPUT_DIR / "final_show.mp3"
    
    # 干音生成 (正常语速)
    await edge_tts.Communicate(cn_txt, VOICE_CN).save(f_cn)
    await edge_tts.Communicate(en_txt, VOICE_EN).save(f_en)
    
    # 混音 & 增益
    cmd = [
        "ffmpeg", "-y", "-i", str(f_cn), "-i", str(f_en),
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[a];[a]volume=1.3[out]",
        "-map", "[out]", str(f_final)
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    f_cn.unlink(); f_en.unlink()
    return f_final

async def send_package(text_brief, audio_path):
    print("📤 Sending Package...")
    t_req = HTTPXRequest(read_timeout=300, write_timeout=300)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
    d = datetime.now().strftime("%Y-%m-%d")
    
    async with app:
        await app.initialize()
        
        # 1. 发送文字简报
        if text_brief:
            # 简单的 markdown 清洗，防止 Gemini 输出不标准的 markdown 导致报错
            safe_text = text_brief.replace("#", "") 
            try:
                await app.bot.send_message(CHAT_ID, text=safe_text, parse_mode=ParseMode.MARKDOWN)
            except:
                # 如果 Markdown 报错，尝试发送纯文本
                await app.bot.send_message(CHAT_ID, text=safe_text)

        # 2. 发送音频
        if audio_path and audio_path.exists():
            with open(audio_path, "rb") as f:
                await app.bot.send_audio(
                    CHAT_ID, f, 
                    caption=f"🎧 Daily News - {d}", 
                    title=f"News {d}", performer="Fredly Bot"
                )
            audio_path.unlink()
            
    print("✅ All Sent!")

# ---------------- RUN ----------------
def job():
    print(f"\n>>> Job: {datetime.now()}")
    news = fetch_rss_news()
    if not news: return
    
    # 生成三个部分：文字稿、中文音源稿、英文音源稿
    txt, cn_aud, en_aud = generate_content(news)
    
    if cn_aud and en_aud:
        # 制作音频
        audio_path = asyncio.run(produce_audio(cn_aud, en_aud))
        # 打包发送
        asyncio.run(send_package(txt, audio_path))
        
    print("<<< End")

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive() # 启动 Web 服务器
    
    print("🚀 Fredly Bot (Text+Audio) Ready")
    
    # 设定定时任务
    schedule.every().day.at("03:00").do(job) # UTC 03:00 = Dubai 07:00

    # 调试模式开关
    if os.getenv("RUN_NOW","false").lower()=="true": job()

    while True:
        schedule.run_pending()
        time.sleep(60)
