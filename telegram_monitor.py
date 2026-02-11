import os
import sys
import re
import time
import json
import io
import subprocess
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ===== 設定スイッチ =====
# ログに掲示板タイトルを表示するかどうか (True: 表示 / False: IDのみ表示)
LOG_WITH_TITLE = False

# ===== 定数・環境変数 =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TARGET_URL = os.environ.get("TARGET_URL")
GITHUB_EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME")

# 必須チェック
missing = []
if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
if not TARGET_URL: missing.append("TARGET_URL")

if missing:
    print(f"Missing environment variables: {', '.join(missing)}")
    sys.exit(1)

# URLリスト化
url_list = [u.strip() for u in TARGET_URL.split(",") if u.strip()]

# User-Agent
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}

# 重複送信防止用
sent_post_ids = set()

# URL検知用 正規表現
URL_PATTERN = re.compile(
    r"https?://[\w/:%#\$&\?\(\)~\.=\+\-]+",
    re.IGNORECASE
)

# ===== 状態管理 (state) =====
def get_board_id(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    return path.split("/")[-1] if path else "default"

def load_last_post_id(board_id: str):
    fname = f"last_post_id_{board_id}.txt"
    if not os.path.exists(fname): return None
    try:
        with open(fname, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return int(content) if content else None
    except Exception: return None

def save_last_post_id(board_id: str, post_id: int):
    fname = f"last_post_id_{board_id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(str(post_id))
    
    # 【追加箇所】GitHubリポジトリにIDファイルを永続保存して重複を完全に防ぐ
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
            subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
            subprocess.run(["git", "add", fname], check=True)
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", f"update last_id {board_id}"], check=True)
                subprocess.run(["git", "push"], check=True)
        except: pass

# ===== ユーティリティ =====
def extract_urls(text: str):
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    filtered_urls = []
    
    # アンカーリンク（/掲示板ID/数字）を判定する正規表現を厳格化
    # 例: https://c.5chan.jp/tYEKGkE0Kj/391
    anchor_regex = re.compile(r'/[a-zA-Z0-9]+/\d+$')

    for url in unique_urls:
        if anchor_regex.search(url):
            # disp や upup.be が含まれない、純粋なアンカーリンクは無視
            if "disp" not in url and "upup.be" not in url:
                continue
                
        if "disp" in url or "upup.be" in url:
            filtered_urls.append(url)
            continue
            
        filtered_urls.append(url)
    return filtered_urls

# ===== Telegram送信ロジック =====
def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    """
    メディアごとに本文とボタンを統合して送信する。
    """
    print(f"      [LOG] 投稿#{post_id} のメディア解析開始 (候補数: {len(media_urls)})")
    valid_media_list = []
    
    for m_url in media_urls:
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        
        # ドメインの動的判定 (cdnc, cdne, cdnf 等に対応)
        netloc_parts = parsed.netloc.split('.')
        d_char = netloc_parts[0]
        
        if d_char.startswith("cdn"):
            base_netloc = parsed.netloc
        else:
            # c, e 等の1文字ドメインを cdnX.5chan.jp に変換
            base_netloc = f"cdn{d_char}.5chan.jp" if len(d_char) == 1 else parsed.netloc

        attempts = [
            {"type": "video", "url": f"https://{base_netloc}/file/{file_id}.mp4", "file_id": file_id},
            {"type": "photo", "url": f"https://{base_netloc}/file/plane/{file_id}.jpg", "file_id": file_id},
            {"type": "photo", "url": f"https://{base_netloc}/file/plane/{file_id}.png", "file_id": file_id}
        ]
        
        found_for_this_url = False
        for attempt in attempts:
            try:
                # HEADではなくGETのstreamで存在をより確実にチェック
                r = requests.get(attempt["url"], headers=headers, stream=True, timeout=10)
                if r.status_code == 200:
                    valid_media
