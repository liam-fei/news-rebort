# ===================== main.py =====================
import os
import sys
import time
from datetime import datetime
from typing import List, Dict

from flask import Flask
from google import genai

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    print("❌ 未检测到 GEMINI_API_KEY")
    sys.exit(1)

TARGET_MINUTES = 3
MODEL_ID = "models/gemini-2.0-flash"  # ✅ 改成 v1 支持的当前主流模型

# ---------------- GEMINI CLIENT (v1 FIXED) ----------------
client = genai.Client(
    api_key=GEMINI_API_KEY,
    http_options={"api_version": "v1"}  # 🔑 保持 v1 稳定版
)

# ---------------- FLASK (KEEP ALIVE / OPTIONAL) ----------------
app = Flask(__name__)

@app.route("/")
def health():
    return "Fredly News Bot is running."

# ---------------- MOCK NEWS FETCHER ----------------
def fetch_articles() -> List[Dict]:
    """
    你可以替换成真实 RSS / API
    这里只放一个最小可运行示例
    """
    return [
        {
            "category": "World",
            "title": "Global markets stabilize amid policy uncertainty",
            "summary": "Markets showed signs of stabilization today as investors reacted cautiously to mixed economic signals."
        },
        {
            "category": "Tech",
            "title": "AI startups attract record investment",
            "summary": "Venture capital funding for AI startups reached a new high, driven by demand for automation tools."
        },
        {
            "category": "Middle East",
            "title": "UAE announces new digital economy initiative",
            "summary": "The initiative aims to boost innovation, attract talent, and expand the country's digital infrastructure."
        }
    ]

# ---------------- SCRIPT GENERATOR ----------------
def generate_script_with_gemini(articles: List[Dict]) -> str | None:
    print("🤖 Gemini 正在生成新闻稿...")
    print(f"🎯 使用模型: {MODEL_ID}")

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
            model=MODEL_ID,
            contents=prompt
        )
        if response and response.text:
            print("✅ Gemini 成功生成新闻稿")
            return response.text
        else:
            print("❌ Gemini 返回空内容")
            return None
    except Exception as e:
        print(f"❌ Gemini 调用失败: {e}")
        return None

# ---------------- MAIN JOB ----------------
def run_job():
    print("Fredly News Bot 已启动")
    print(f">>> 任务开始: {datetime.now()}")

    print("📡 抓取新闻源...")
    articles = fetch_articles()
    print(f"✅ 抓取 {len(articles)} 篇文章")

    script = generate_script_with_gemini(articles)
    if not script:
        print("❌ 新闻稿生成失败，任务终止")
        return

    print("\n========== 生成的新闻稿 ==========\n")
    print(script)
    print("\n========== END ==========\n")

# ---------------- ENTRY ----------------
if __name__ == "__main__":
    run_job()
    # 如需 Flask 常驻，取消下面注释
    # app.run(host="0.0.0.0", port=8080)
