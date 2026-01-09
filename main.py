# =============================================
# Fredly News Bot - VERIFICATION MODE (Dry Run)
# 作用：只生成文案并打印，不生成音频，不发 Telegram
# 用于验证：选题逻辑、搜索质量、文案风格
# =============================================

import os
import sys
import time
import json
import re
import logging
from datetime import datetime
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
# 验证模式下其实不需要 TG Token，但为了保持代码一致性先留着检查
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

if not GEMINI_API_KEY:
    log.error("❌ Missing GEMINI_API_KEY")
    sys.exit(1)

BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
TARGET_MINUTES = 13

# ---------------- HTTP SESSION ----------------
def make_session():
    s = requests.Session()
    retries = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "POST"]
    )
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s

SESSION = make_session()

# ---------------- GEMINI ENGINE (Auth Fixed) ----------------
def get_api_url():
    # 使用 URL 参数鉴权，防止 401 错误
    url = f"{BASE_URL}/models?key={GEMINI_API_KEY}"
    try:
        r = SESSION.get(url, timeout=10)
        if r.status_code != 200:
            log.error(f"❌ API Error: {r.status_code} {r.text}")
            return None
            
        models = r.json().get("models", [])
        candidates = [m["name"] for m in models if "generateContent" in m.get("supportedGenerationMethods", [])]
        
        # 逻辑能力优先
        priority = ["gemini-2.0-pro", "gemini-1.5-pro", "gemini-2.5", "gemini-2.0-flash", "gemini-1.5-flash"]
        chosen = next((m for p in priority for m in candidates if p in m), candidates[0] if candidates else None)
            
        if chosen:
            log.info(f"✅ Logic Engine Selected: {chosen}")
            return f"{BASE_URL}/{chosen}:generateContent"
    except Exception as e:
        log.error(f"Model discovery failed: {e}")
    return None

def call_gemini(prompt, base_url, json_mode=False):
    url = f"{base_url}?key={GEMINI_API_KEY}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2} # 低温，严谨逻辑
    }
    if json_mode:
        payload["generationConfig"]["responseMimeType"] = "application/json"

    try:
        r = SESSION.post(url, headers=headers, json=payload, timeout=100)
        if r.status_code != 200:
            log.error(f"Gemini Call Failed: {r.text}")
            return None
        return r.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        log.error(f"Gemini Network Error: {e}")
        return None

# ---------------- PIPELINE STEPS ----------------

def step1_scan_headlines():
    log.info("📡 [Step 1] Scanning Global Headlines...")
    url = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
    try:
        d = feedparser.parse(url)
        titles = [e.get("title", "").split(" - ")[0] for e in d.entries[:50]]
        log.info(f"   -> Fetched {len(titles)} raw headlines.")
        return titles
    except Exception as e:
        log.error(f"RSS failed: {e}")
        return []

def step2_select_topics(headlines, api_url):
    log.info("🧠 [Step 2] AI Editor Selecting Top 5 Topics...")
    prompt = (
        "Role: Chief Editor.\n"
        "Task: Analyze these 50 headlines and identify the TOP 5 most significant global topics for today.\n"
        "Criteria: Choose viral, high-impact, or major market-moving stories. Group related headlines.\n"
        "Output: A strictly JSON array of search strings (e.g. ['Topic A', 'Topic B']).\n"
        "Headlines Pool:\n" + "\n".join(headlines)
    )
    raw = call_gemini(prompt, api_url, json_mode=True)
    if not raw: return []
    
    try:
        clean = raw.replace("```json", "").replace("```", "").strip()
        topics = json.loads(clean)
        return topics
    except Exception as e:
        log.error(f"JSON parse error: {e}")
        return []

def fetch_topic_details(topic):
    # 多线程 Worker
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(topic)}&hl=en-US&gl=US&ceid=US:en"
    try:
        d = feedparser.parse(url)
        block = f"### TOPIC: {topic}\n"
        for e in d.entries[:3]: # 每个话题抓3篇
            summary = re.sub("<[^<]+?>", "", e.get("summary", ""))[:300]
            src = e.get("source", {}).get("title", "Unknown")
            block += f"- Source ({src}): {summary}\n"
        return block
    except:
        return f"### TOPIC: {topic} (Fetch Failed)\n"

def step3_deep_research(topics):
    log.info(f"🕵️ [Step 3] Deep Researching {len(topics)} topics (Parallel)...")
    results = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(fetch_topic_details, t) for t in topics]
        for f in as_completed(futures):
            results.append(f.result())
    return "\n".join(results)

def step4_write_scripts(research_data, api_url):
    log.info("✍️ [Step 4] Writing Scripts (Verification)...")
    today = datetime.now().strftime("%Y-%m-%d")

    # 1. 文字简报
    p_brief = (
        f"Role: Editor. Date: {today}.\n"
        f"Task: Write a Telegram Markdown summary based on research.\n"
        f"Format:\n📅 **早安简报 {today}**\n\n🔥 **今日五大热点**\n1. **[Title]** - [One sentence key takeaway]\n...\n"
        f"Research Data:\n{research_data}"
    )
    text = call_gemini(p_brief, api_url)

    # 2. 中文导语 (央视风)
    p_cn = (
        f"Role: Professional News Anchor. Date: {today}.\n"
        f"Task: Spoken Chinese Intro. Style: CCTV News / Formal.\n"
        f"Structure:\n"
        f"1. Start: '这里是Fredly早间新闻。今天是{today}。'\n"
        f"2. Summarize the single biggest event first.\n"
        f"3. Briefly mention other key topics.\n"
        f"4. End: '以下是详细英文报道。'\n"
        f"Note: Be concise. No 'First/Second'. Flow like a story.\n"
        f"Data:\n{research_data}"
    )
    cn = call_gemini(p_cn, api_url)

    # 3. 英文正文 (BBC风)
    p_en = (
        f"Role: Senior Correspondent. Task: {TARGET_MINUTES}-minute deep news report.\n"
        f"Style: BBC/Reuters. Formal, Objective, Analytical.\n"
        f"Structure:\n"
        f"1. NO INTRO/GREETING. Start directly with the biggest story details.\n"
        f"2. Cover all 5 topics in depth. Use sources provided (e.g., 'Reuters reports that...').\n"
        f"3. Smooth transitions between topics.\n"
        f"Length: ~1600 words.\n"
        f"Data:\n{research_data}"
    )
    en = call_gemini(p_en, api_url)

    return text, cn, en

# ---------------- VERIFICATION RUNNER ----------------
def run_verification():
    log.info("🧪 STARTING CONTENT VERIFICATION (DRY RUN)")
    
    # 1. API Check
    api_url = get_api_url()
    if not api_url:
        log.error("❌ API Config Failed")
        return

    # 2. Scan
    headlines = step1_scan_headlines()
    if not headlines:
        log.error("❌ No headlines found")
        return

    # 3. Select
    topics = step2_select_topics(headlines, api_url)
    log.info(f"🔹 AI Selected Topics:\n{json.dumps(topics, indent=2, ensure_ascii=False)}")
    if not topics: return

    # 4. Research
    research = step3_deep_research(topics)
    # 打印部分调研资料看看质量
    print(f"\n📝 --- RESEARCH DATA PREVIEW (First 500 chars) ---\n{research[:500]}...\n----------------------------------\n")

    # 5. Write
    text, cn, en = step4_write_scripts(research, api_url)

    # 6. Show Results
    print("\n" + "="*50)
    print("📢 [RESULT 1] TELEGRAM BRIEF (Markdown)")
    print("="*50)
    print(text)

    print("\n" + "="*50)
    print("🇨🇳 [RESULT 2] CHINESE INTRO (Script)")
    print("="*50)
    print(cn)

    print("\n" + "="*50)
    print("🇬🇧 [RESULT 3] ENGLISH DEEP DIVE (Script)")
    print("="*50)
    print(en)
    
    log.info("✅ Verification Complete. Check the text above.")

if __name__ == "__main__":
    # 直接运行验证，不启动 Schedule
    run_verification()
