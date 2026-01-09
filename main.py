# =============================================
# Fredly News Bot - Global & China Focus (24H Strict)
# 特性：强制包含中国热点 + 严格24小时过滤 + 去美国化
# 模式：验证模式 (只输出文本日志，不发TG/音频)
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ---------------- LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("Fredly_Verify")

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
# 验证模式下可为空
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") 
CHAT_ID = os.getenv("CHAT_ID")

if not GEMINI_API_KEY:
    log.error("❌ Missing GEMINI_API_KEY")
    sys.exit(1)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TARGET_MINUTES = 13

# ---------------- RSS SOURCES (Global + China) ----------------
# 使用 gl=GB (英国版) 和 gl=SG (新加坡版) 来获取更国际化和亚洲视角的报道
RSS_POOLS = {
    # 1. 全球头条 (英国版 - 偏向BBC/路透)
    "WORLD_TOP": "https://news.google.com/rss?hl=en-GB&gl=GB&ceid=GB:en",
    
    # 2. 中国专题 (强制搜索 'China' 且限定 when:1d 过去24小时)
    "CHINA_HOT": "https://news.google.com/rss/search?q=China+when:1d&hl=en-GB&gl=GB&ceid=GB:en",
    
    # 3. 半岛电视台 (全球南方视角)
    "AL_JAZEERA": "https://www.aljazeera.com/xml/rss/all.xml"
}

# ---------------- HTTP SESSION ----------------
def make_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502], allowed_methods=["GET", "POST"])
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

SESSION = make_session()

# ---------------- UTILS: TIME FILTER ----------------
def is_recent(entry, hours=24):
    """
    物理级时间过滤：严格丢弃超过 24 小时的旧闻
    """
    # 1. Google News 通常有 published_parsed
    if hasattr(entry, 'published_parsed') and entry.published_parsed:
        try:
            pub_time = datetime.fromtimestamp(mktime(entry.published_parsed))
            # 允许一点时区误差，设定为 25 小时
            if datetime.now() - pub_time < timedelta(hours=25):
                return True
            else:
                return False # 太旧了
        except:
            pass # 解析失败往下走
            
    # 2. 如果没有时间戳，检查标题里是否有 "Live", "Just now" 等词 (可选)
    # 为了严格起见，没有时间戳的如果是 Google News 来源，最好丢弃，防止 2008 年旧闻
    # 但 Al Jazeera 有时时间戳格式不同，这里我们默认：如果解析不到时间，且是置顶新闻，暂且放行，靠 Prompt 二次清洗
    return True 

# ---------------- GEMINI ENGINE ----------------
def get_api_url():
    url = f"{BASE_URL}/models?key={GEMINI_API_KEY}"
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200: return None
        models = r.json().get("models", [])
        cands = [m["name"] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
        priority = ["gemini-2.0-pro", "gemini-1.5-pro", "gemini-2.5", "gemini-2.0-flash"]
        chosen = next((m for p in priority for m in cands if p in m), cands[0] if cands else None)
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
        "generationConfig": {"temperature": 0.2} 
    }
    if json_mode: payload["generationConfig"]["responseMimeType"] = "application/json"
    
    try:
        r = SESSION.post(url, headers=headers, json=payload, timeout=100)
        if r.status_code != 200:
            log.error(f"Gemini Error: {r.text}")
            return None
        return r.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        log.error(f"Gemini Net Error: {e}")
        return None

# ---------------- PIPELINE ----------------

def step1_scan_headlines():
    log.info("📡 [Step 1] Scanning Global & China Feeds (Strict 24H)...")
    combined_titles = []
    
    for category, url in RSS_POOLS.items():
        try:
            d = feedparser.parse(url)
            count = 0
            for e in d.entries:
                if is_recent(e, hours=24):
                    # 给标题加上前缀，方便 AI 识别来源
                    clean_title = e.get("title", "").split(" - ")[0]
                    # 如果来自中国源，加个标记强提示
                    prefix = "[CHINA NEWS]" if category == "CHINA_HOT" else "[GLOBAL]"
                    combined_titles.append(f"{prefix} {clean_title}")
                    count += 1
                if count >= 15: break # 每个源取最新15条
        except Exception as e:
            log.error(f"Feed error {category}: {e}")

    random.shuffle(combined_titles)
    # 只要前 60 条，防止 Token 溢出
    final_list = combined_titles[:60]
    log.info(f"   -> Found {len(final_list)} fresh headlines.")
    return final_list

def step2_select_topics(headlines, api_url):
    log.info("🧠 [Step 2] AI Selecting 5 Events (Must Include China)...")
    
    # 🔥 Prompt 强约束：必须包含中国，必须是具体事件
    prompt = (
        f"Role: Chief Editor. Current Date: {datetime.now().strftime('%Y-%m-%d')}\n"
        "Task: Select Top 5 BREAKING NEWS EVENTS from the list.\n"
        "MANDATORY REQUIREMENTS:\n"
        "1. ✅ MUST include at least 1 event related to CHINA (look for [CHINA NEWS] tag).\n"
        "2. ✅ Select only CONCRETE EVENTS (e.g. 'SpaceX Launch', 'Earthquake in Japan').\n"
        "3. ❌ IGNORE general topics (e.g. 'Technology trends', 'Climate Change').\n"
        "4. ❌ IGNORE anything that looks like old news (2008 crisis, etc).\n"
        "Output: JSON array of search queries.\n"
        "Headlines Pool:\n" + "\n".join(headlines)
    )
    
    raw = call_gemini(prompt, api_url, json_mode=True)
    if not raw: return []
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        topics = json.loads(clean)
        log.info(f"🔹 Selected: {topics}")
        return topics
    except: return []

def fetch_topic_details(topic):
    # 🔥 核心：搜索时加 "when:1d" 强制 Google 只给 24小时内数据
    # 🔥 核心：gl=GB 使用英国版，避免美国视角
    search_query = f"{topic} when:1d"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(search_query)}&hl=en-GB&gl=GB&ceid=GB:en"
    
    try:
        d = feedparser.parse(url)
        if not d.entries: return "" # 搜不到直接空，触发熔断
        
        block = f"### EVENT: {topic}\n"
        valid_count = 0
        for e in d.entries:
            if is_recent(e, hours=24):
                summary = re.sub("<[^<]+?>", "", e.get("summary", ""))[:350]
                src = e.get("source", {}).get("title", "Unknown")
                pub = e.get("published", "")
                block += f"- [{pub}] {src}: {summary}\n"
                valid_count += 1
                if valid_count >= 3: break
        
        return block if valid_count > 0 else ""
    except: return ""

def step3_deep_research(topics):
    log.info(f"🕵️ [Step 3] Deep Researching {len(topics)} events...")
    results = []
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = [ex.submit(fetch_topic_details, t) for t in topics]
        for f in as_completed(futures):
            res = f.result()
            if res: results.append(res)
            
    if not results:
        log.error("❌ No valid news found after strict filtering!")
        return None
    return "\n".join(results)

def step4_write_scripts(research_data, api_url):
    log.info("✍️ [Step 4] Writing Scripts (Anti-Hallucination)...")
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 简报
    p_brief = (
        f"Role: Editor. Date: {today}.\n"
        f"Task: Write Telegram Markdown summary.\n"
        f"Rule: ONLY use facts from Data. MUST cover the China story.\n"
        f"Format:\n📅 **早安简报 {today}**\n\n🔥 **今日五大热点**\n1. **[Headline]** - [Detail]\n...\n"
        f"Data:\n{research_data}"
    )
    text = call_gemini(p_brief, api_url)

    # 2. 中文导语 (央视风)
    p_cn = (
        f"Role: Anchor. Date: {today}. Style: CCTV News.\n"
        f"Task: Spoken Intro. Cover top stories including China.\n"
        f"Rule: No 'First/Second'. Be concise. NO hallucinations.\n"
        f"Start: '这里是Fredly早间新闻。今天是{today}。'\n"
        f"Data:\n{research_data}"
    )
    cn = call_gemini(p_cn, api_url)

    # 3. 英文正文 (BBC风)
    p_en = (
        f"Role: Senior Correspondent. Task: {TARGET_MINUTES}-minute report.\n"
        f"Style: BBC/Al Jazeera. International perspective.\n"
        f"RULES:\n"
        f"1. CITE SOURCES (e.g. 'According to BBC...').\n"
        f"2. NO INTRO. Start with the most impactful story.\n"
        f"3. Ensure the China-related story is covered in depth.\n"
        f"Data:\n{research_data}"
    )
    en = call_gemini(p_en, api_url)
    return text, cn, en

# ---------------- RUNNER ----------------
def run_verification():
    log.info("🧪 STARTING VERIFICATION (Strict 24H + China Focus)")
    api_url = get_api_url()
    if not api_url: return

    # 1. Scan
    headlines = step1_scan_headlines()
    if not headlines: 
        log.error("❌ No headlines found. Check network.")
        return

    # 2. Select
    topics = step2_select_topics(headlines, api_url)
    if not topics: return

    # 3. Research
    research = step3_deep_research(topics)
    if not research: return
    
    print(f"\n📝 RESEARCH DATA SAMPLE:\n{research[:500]}...\n")

    # 4. Write
    text, cn, en = step4_write_scripts(research, api_url)
    
    print("\n" + "="*40 + "\n📢 TELEGRAM BRIEF\n" + "="*40)
    print(text)
    print("\n" + "="*40 + "\n🇨🇳 CHINESE INTRO\n" + "="*40)
    print(cn)
    print("\n" + "="*40 + "\n🇬🇧 ENGLISH SCRIPT\n" + "="*40)
    print(en[:1500] + "...\n")
    
    log.info("✅ Verification Complete.")

if __name__ == "__main__":
    # 直接运行验证
    run_verification()
