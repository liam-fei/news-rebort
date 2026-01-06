# =============================================
# Fredly News Bot - Low Memory & High Traffic
# 特性：FFmpeg流式混音 (防崩溃) + 40篇昨日热榜
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
import subprocess  # 引入子进程，用于直接调用 FFmpeg
from datetime import datetime, timedelta
from pathlib import Path
from telegram.ext import Application
from telegram.request import HTTPXRequest

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
TARGET_MINUTES = 13
CANDIDATE_POOL_SIZE = 40 

# BGM: Lofi Hip Hop
BGM_URL = "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3?filename=lofi-study-112191.mp3"

# Google News 聚合源 (昨日热点)
RSS_FEEDS = {
    "Global": ["https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"],
    "Tech": ["https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=en-US&gl=US&ceid=US:en"],
    "Business": ["https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"],
    "Science": ["https://news.google.com/rss/headlines/section/topic/SCIENCE?hl=en-US&gl=US&ceid=US:en"]
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
        if chosen: 
            print(f"✅ Model: {chosen}")
            return f"{BASE_URL}/{chosen}:generateContent?key={GEMINI_API_KEY}"
    except: pass
    return None

def call_gemini(prompt, url):
    try:
        r = requests.post(url, headers={'Content-Type':'application/json'}, data=json.dumps({"contents":[{"parts":[{"text":prompt}]}]}), timeout=90)
        if r.status_code==200: return r.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e: print(f"Gemini Err: {e}")
    return None

# ---------------- 2. FETCH (High Volume) ----------------
def fetch_rss_news():
    print("\n📡 Fetching Top Headlines...")
    articles = []
    seen_titles = set()
    
    for cat, feeds in RSS_FEEDS.items():
        for url in feeds:
            if len(articles) >= CANDIDATE_POOL_SIZE: break
            try:
                d = feedparser.parse(url)
                # 每个源取前 10 条
                for entry in d.entries[:10]: 
                    title = entry.get("title", "").split(" - ")[0]
                    if title and title not in seen_titles:
                        articles.append(f"[{cat}] {title}")
                        seen_titles.add(title)
            except: pass
            
    print(f"✅ Collected {len(articles)} headlines.")
    return articles

def generate_scripts(articles):
    url = get_api_url()
    if not url: return None, None
    
    news_text = "\n".join(articles)
    yesterday_str = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    print("🤖 Selecting Yesterday's Top Stories...")

    # 中文导语
    p_cn = (
        f"Role: News Editor. Context: Today is {datetime.now().strftime('%Y-%m-%d')}. "
        f"Task: Select Top 5 stories from YESTERDAY ({yesterday_str}). "
        f"Output: Spoken CHINESE intro. "
        f"1. Start: '大家早上好，今天是[Date]。回顾昨天全球大事...'\n"
        f"2. Summarize top stories.\n"
        f"3. End: '接下来请听 Sara 的深度英文分析。'\n"
        f"Headlines: {news_text}"
    )
    cn = call_gemini(p_cn, url)

    # 英文正文
    print("🤖 Writing Deep Dive Analysis...")
    p_en = (
        f"Role: Sara, news analyst. Task: {TARGET_MINUTES}-minute 'Daily Recap' script in ENGLISH. "
        f"Focus: Recap PAST 24 HOURS. "
        f"Structure: Intro -> The Big Story (4 mins) -> Tech/Markets -> Rapid Recap -> Outro. "
        f"Tone: Analytical. Length: ~1800 words.\n"
        f"Headlines: {news_text}"
    )
    en = call_gemini(p_en, url)
    return cn, en

# ---------------- 3. PRODUCTION (Low Memory) ----------------
async def produce_show(cn_txt, en_txt):
    if not ensure_ffmpeg(): return None
    print("🎙️ Audio Production (Stream Mode)...")
    
    # 路径定义
    f_cn = OUTPUT_DIR / "part1.mp3"
    f_en = OUTPUT_DIR / "part2.mp3"
    f_bgm = OUTPUT_DIR / "bgm.mp3"
    f_final = OUTPUT_DIR / "final_show.mp3"
    
    # 1. 生成干音
    await edge_tts.Communicate(cn_txt, VOICE_CN).save(f_cn)
    await edge_tts.Communicate(en_txt, VOICE_EN, rate="-5%").save(f_en)
    
    # 2. 下载 BGM
    if not f_bgm.exists():
        print("   Downloading BGM...")
        with open(f_bgm, "wb") as f:
            f.write(requests.get(BGM_URL).content)

    print("🎚️ Mixing via FFmpeg (Memory Safe)...")
    
    # 🔥 核心修改：使用 FFmpeg 命令行直接混音，不使用 Pydub 加载到内存
    # 逻辑：[0]+[1] 拼接语音 -> [2] BGM 循环并降低音量 -> 混合
    cmd = [
        "ffmpeg", "-y",
        "-i", str(f_cn),  # 输入0: 中文
        "-i", str(f_en),  # 输入1: 英文
        "-stream_loop", "-1", "-i", str(f_bgm), # 输入2: BGM (无限循环)
        "-filter_complex",
        # 1. 拼接中文和英文 (n=2:v=0:a=1)，中间稍微停顿一下比较难写，直接硬拼
        "[0:a][1:a]concat=n=2:v=0:a=1[voice];" 
        # 2. 处理 BGM: 音量减小 (volume=0.1)
        "[2:a]volume=0.1[bgm];"
        # 3. 混合: 语音流和BGM流，duration=first (以语音长度为准)
        "[voice][bgm]amix=inputs=2:duration=first:dropout_transition=2[out]",
        "-map", "[out]",
        str(f_final)
    ]

    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("✅ Mixing Complete!")
        
        # 清理临时文件
        f_cn.unlink()
        f_en.unlink()
        return f_final
    except Exception as e:
        print(f"❌ FFmpeg Error: {e}")
        return None

async def send_tg(path):
    print("📤 Sending...")
    t_req = HTTPXRequest(read_timeout=300, write_timeout=300)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
    d = datetime.now().strftime("%Y-%m-%d")
    async with app:
        await app.initialize()
        with open(path, "rb") as f:
            await app.bot.send_audio(
                CHAT_ID, f, 
                caption=f"🔥 Yesterday's Top Stories - {d}", 
                title=f"Daily Recap {d}", performer="Fredly Bot"
            )
    path.unlink()
    print("✅ Sent!")

# ---------------- RUN ----------------
def job():
    print(f"\n>>> Job: {datetime.now()}")
    news = fetch_rss_news()
    if not news: return
    cn, en = generate_scripts(news)
    if cn and en:
        path = asyncio.run(produce_show(cn, en))
        if path: asyncio.run(send_tg(path))
    print("<<< End")

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()
    print("🚀 Fredly Bot (Low Memory Edition) Ready")
    schedule.every().day.at("03:00").do(job)
    if os.getenv("RUN_NOW","false").lower()=="true": job()
    while 1: schedule.run_pending(); time.sleep(60)
