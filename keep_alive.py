from flask import Flask
from threading import Thread
import os
import logging

# 禁止 Flask 输出多余的日志，保持控制台清爽
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask('')

@app.route('/')
def home():
    return "✅ Fredly News Bot is Running! (Gemini + EdgeTTS)", 200

def run():
    # Render 会自动提供 PORT 环境变量
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run)
    t.daemon = True
    t.start()
