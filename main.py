# =============================================
# Fredly News Bot - GOLDEN BALANCE EDITION
# 修复：解决“太无聊”的问题
# 策略：从“机械读稿”升级为“资深主播叙事” (Engaging but Factual)
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
log = logging.getLogger("Fredly_Golden")

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
        # 🔥 温度回调到 0.3：保持事实，但语言更流畅、自然，不那么生硬
        "generationConfig": {"temperature": 0.3} 
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
    prompt = (
        f"Role: Chief Editor. Date: {today}\n"
        "Task: Select Top 5 HEADLINES.\n"
        "SELECTION CRITERIA:\n"
        "1. ✅ MUST include
