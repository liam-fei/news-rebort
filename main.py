# =============================================
# Fredly News Bot - Ultimate Commute Edition
# 特性：中文导语(中文声) + 英文深度(英文声) + 自动拼接 + BGM
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
from datetime import datetime
from pathlib import Path
from telegram.ext import Application
from telegram.request import HTTPXRequest
from pydub import AudioSegment

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not all([GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, CHAT_ID]):
    print("❌ Error: Missing Environment Variables")
    sys.exit(1)

# API 设置
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

# --- 配音员设置 ---
# 中文导语声优 (知性女声)
VOICE_CN = "zh-CN-XiaoxiaoNeural"
# 英文主播声优 (Sara)
VOICE_EN = "en-US-AvaNeural"

TARGET_MINUTES = 12 # 设定英文部分约12-13分钟，加上中文刚好15分钟内
ARTICLES_LIMIT = 4

# BGM: 舒缓的 Lofi (开车听很舒服)
BGM_URL = "https://cdn.pixabay.com/download/audio/2022/05/27/audio_1808fbf07a.mp3?filename=lofi-study-112191.mp3"

RSS_FEEDS = {
    "Global": ["http://feeds.bbci.co.uk/news/rss.xml", "http://rss.cnn.com/rss/edition.rss"],
    "Tech": ["https://techcrunch.com/feed/"],
    "Business": ["https://feeds.bloomberg.com/markets/news.rss"],
    "Life": ["https://www.wired.com/feed/rss"]
}

OUTPUT_DIR = Path("./outputs")
OUTPUT_DIR.mkdir(exist_ok=True)
BIN_DIR = Path("./bin")
BIN_DIR.mkdir(exist_ok=True)

# ---------------- 0. FFmpeg Auto-Setup ----------------
def ensure_ffmpeg():
    """自动安装 FFmpeg (混音和拼接必需)"""
    ffmpeg_path = BIN_DIR / "ffmpeg"
    if ffmpeg_path.exists():
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.absolute())
        return True

    print("🛠️ Installing FFmpeg static build...")
    try:
        url = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        response = requests.get(url, stream=True)
        tar_path = BIN_DIR / "ffmpeg.tar.xz"
        with open(tar_path, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        
        with tarfile.open(tar_path, "r:xz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/ffmpeg"):
                    member.name = "ffmpeg"
                    tar.extract(member, path=BIN_DIR)
                    break
        
        (BIN_DIR / "ffmpeg").chmod(0o755)
        os.environ["PATH"] += os.pathsep + str(BIN_DIR.absolute())
        tar_path.unlink()
        print("✅ FFmpeg installed!")
        return True
    except Exception as e:
        print(f"❌ FFmpeg install failed: {e}")
        return False

# ---------------- 1. SMART MODEL FINDER ----------------
def get_api_url():
    """获取可用 Gemini URL"""
    print("🔍 Auto-detecting models...")
    try:
        resp = requests.get(f"{BASE_URL}/models?key={GEMINI_API_KEY}", timeout=10)
        if resp.status_code != 200: return None
        candidates = [m['name'] for m in resp.json().get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
        
        # 优先 2.5/2.0，其次 1.5
        priority = ['gemini-2.5', 'gemini-2.0-flash', 'gemini-1.5-flash', 'gemini-1.5-pro']
        chosen = next((m for p in priority for m in candidates if p in m), candidates[0] if candidates else None)
        
        if chosen:
            print(f"✅ Model: {chosen}")
            return f"{BASE_URL}/{chosen}:generateContent?key={GEMINI_API_KEY}"
    except: pass
    return None

# ---------------- 2. CONTENT GENERATION ----------------
def call_gemini(prompt, model_url):
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        resp = requests.post(model_url, headers={'Content-Type': 'application/json'}, data=json.dumps(payload), timeout=60)
        if resp.status_code == 200:
            return resp.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        print(f"Gemini Error: {e}")
    return None

def fetch_rss_news():
    print("\n📡 Fetching RSS...")
    articles = []
    seen_titles = set()
    for cat, feeds in RSS_FEEDS.items():
        for url in feeds:
            if len(articles) >= 12: break
            try:
                d = feedparser.parse(url)
                for entry in d.entries[:1]:
                    title = entry.get("title", "")
                    if title not in seen_titles:
                        articles.append(f"[{cat}] {title}: {entry.get('summary', '')[:300]}")
                        seen_titles.add(title)
            except: pass
    print(f"✅ Got {len(articles)} articles")
    return articles

def generate_scripts(articles):
    url = get_api_url()
    if not url: return None, None
    articles_text = "\n".join(articles)

    # --- Part 1: 中文导语 ---
    print("🤖 Generating Chinese Intro...")
    intro_prompt = (
        f"You are a news assistant. Create a spoken introduction in CHINESE based on these articles.\n"
        f"Format requirements:\n"
        f"1. Start with '大家早上好，今天是[Date]。'\n"
        f"2. Summarize the top 3-4 most important headlines in one sentence each (e.g. '今天的重点新闻有：XXX，以及XXX...').\n"
        f"3. End with exactly: '接下来请听 Sara 为您带来的详细英文报道。'\n"
        f"Keep it under 1 minute. Natural spoken Chinese.\n\n"
        f"Articles: {articles_text}"
    )
    cn_script = call_gemini(intro_prompt, url)

    # --- Part 2: 英文正文 ---
    print("🤖 Generating English Deep Dive...")
    main_prompt = (
        f"Role: Sara, a news anchor. \n"
        f"Task: Create a {TARGET_MINUTES}-minute news script in ENGLISH.\n"
        f"Start immediately with 'Hello, I'm Sara. Let's dive into the stories.' (Do not repeat the date).\n"
        f"Cover the provided articles in depth. Use transitions. Be engaging.\n"
        f"Total word count aim: ~1600 words.\n"
        f"Plain text only.\n\n"
        f"Articles: {articles_text}"
    )
    en_script = call_gemini(main_prompt, url)

    return cn_script, en_script

# ---------------- 3. AUDIO PRODUCTION ----------------

async def produce_radio_show(cn_text, en_text):
    if not ensure_ffmpeg(): return None

    print("🎙️ Production Start...")
    
    # 1. 生成中文导语 (Xiaoxiao)
    path_cn = OUTPUT_DIR / "intro_cn.mp3"
    await edge_tts.Communicate(cn_text, VOICE_CN).save(path_cn)
    
    # 2. 生成英文正文 (Sara/Ava) - 语速 -5% 适合通勤
    path_en = OUTPUT_DIR / "main_en.mp3"
    await edge_tts.Communicate(en_text, VOICE_EN, rate="-5%").save(path_en)

    # 3. 拼接音频
    print("🎚️ Splicing Audio...")
    seg_cn = AudioSegment.from_file(path_cn)
    seg_en = AudioSegment.from_file(path_en)
    # 中间加 1 秒空白停顿
    silence = AudioSegment.silent(duration=1000) 
    combined_voice = seg_cn + silence + seg_en

    # 4. 混入 BGM
    print("🎵 Mixing BGM...")
    bgm_path = OUTPUT_DIR / "bgm.mp3"
    if not bgm_path.exists():
        r = requests.get(BGM_URL)
        with open(bgm_path, "wb") as f: f.write(r.content)
    
    bgm = AudioSegment.from_file(bgm_path)
    # BGM 音量降低 19dB (确保人声清晰)
    bgm = bgm - 19
    
    # 循环 BGM 直到覆盖全长
    target_len = len(combined_voice) + 4000
    while len(bgm) < target_len:
        bgm += bgm
    bgm = bgm[:target_len]
    bgm = bgm.fade_in(2000).fade_out(3000)

    # 混合: BGM 在人声开始前 0.5秒淡入
    final_mix = bgm.overlay(combined_voice, position=500)

    # 导出
    final_path = OUTPUT_DIR / "daily_show.mp3"
    final_mix.export(final_path, format="mp3")
    
    # 清理中间文件
    path_cn.unlink()
    path_en.unlink()
    
    return final_path

async def upload_telegram(mp3_path):
    print("📤 Uploading to Telegram...")
    t_req = HTTPXRequest(read_timeout=300.0, write_timeout=300.0)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(t_req).build()
    
    date_str = datetime.now().strftime("%Y-%m-%d")
    async with app:
        await app.initialize()
        with open(mp3_path, "rb") as f:
            await app.bot.send_audio(
                chat_id=CHAT_ID, 
                audio=f, 
                caption=f"🚗 Morning News Drive - {date_str}",
                title=f"Daily Briefing {date_str}",
                performer="Fredly Bot"
            )
    print("✅ Done!")
    mp3_path.unlink()

# ---------------- JOB ----------------
def run_job():
    print(f"\n>>> Job Started: {datetime.now()}")
    news = fetch_rss_news()
    if not news: return
    
    cn, en = generate_scripts(news)
    if not cn or not en: return
    
    final_mp3 = asyncio.run(produce_radio_show(cn, en))
    if final_mp3:
        asyncio.run(upload_telegram(final_mp3))
    print("<<< Job Finished\n")

if __name__ == "__main__":
    from keep_alive import keep_alive
    keep_alive()

    print(f"🚀 Fredly News Bot (Ultimate Edition) Ready")
    schedule.every().day.at("03:00").do(run_job)

    if os.getenv("RUN_NOW", "false").lower() == "true":
        run_job()

    while True:
        schedule.run_pending()
        time.sleep(60)
