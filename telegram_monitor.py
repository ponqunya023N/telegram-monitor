import os
import sys
import re
import time
import json
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ===== 定数・環境変数 =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TARGET_URL = os.environ.get("TARGET_URL")
GITHUB_EVENT_NAME = os.environ.get("GITHUB_EVENT_NAME")

# 検証用：特定の掲示板と番号を強制的に狙い撃つ設定
DEBUG_TARGETS = {
    "2deSYWUkc5": 861,
    "tYEKGkE0Kj": 32
}

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

# 1回のアクション実行で送信済みのIDを記録
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

# ===== ユーティリティ =====
def extract_urls(text: str):
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    filtered_urls = []
    for url in unique_urls:
        parsed = urlparse(url)
        path = parsed.path.rstrip("/")
        last_segment = path.split("/")[-1] if "/" in path else ""
        if "disp" in url or "upup.be" in url:
            filtered_urls.append(url)
            continue
        if last_segment.isdigit(): continue
        filtered_urls.append(url)
    return filtered_urls

# ===== Telegram送信ロジック =====
def send_telegram_media_group(board_name, board_id, post_id, posted_at, body_text, target_post_url, media_urls):
    """
    メディアをグループ（アルバム）として送信し、
    その直後に詳細情報と「ブラウザで開く」ボタンを送信する。
    """
    print(f"      [DEBUG] Telegramへ送信を試みます... (Media: {len(media_urls)})")
    
    # 1. メディアの準備 (最大10枚)
    media_group = []
    processed_count = 0
    
    for m_url in media_urls:
        if processed_count >= 10: break
        
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        d_char = parsed.netloc.split('.')[0]
        # cdnX.5chan.jp 形式への補正
        base_netloc = parsed.netloc if d_char.startswith("cdn") else f"cdn{d_char}.5chan.jp"

        # 試行URLリスト（画像優先 -> 動画）
        attempt_urls = [
            f"https://{base_netloc}/file/plane/{file_id}.jpg",
            f"https://{base_netloc}/file/{file_id}.mp4",
            f"https://{base_netloc}/file/plane/{file_id}.png",
            f"https://{base_netloc}/file/{file_id}.gif"
        ]
        # 元のURLに拡張子が含まれている場合は最優先
        if "." in file_id: attempt_urls.insert(0, m_url)

        for target_download_url in attempt_urls:
            try:
                # HEADリクエストで存在確認
                r = requests.head(target_download_url, headers=headers, timeout=10)
                if r.status_code == 200:
                    # Content-Type または拡張子から種別判断
                    content_type = r.headers.get('Content-Type', '').lower()
                    ext = target_download_url.split('.')[-1].lower()
                    
                    if "video" in content_type or ext in ["mp4", "mov", "webm"]:
                        media_type = "video"
                    else:
                        media_type = "photo"
                        
                    media_group.append({"type": media_type, "media": target_download_url})
                    processed_count += 1
                    break
            except: continue

    # 2. メディアグループの送信
    if media_group:
        send_group_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
        # 最初のメディアにのみキャプションをつけることも可能ですが、別途メッセージを送るためここでは送信のみ
        requests.post(send_group_url, data={"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media_group)})
    else:
        print(f"      [DEBUG] 有効なメディアファイルURLが見つかりませんでした。")

    # 3. テキストとインラインボタンの送信
    # 冒頭300文字程度を引用
    summary_text = body_text[:300] + ("..." if len(body_text) > 300 else "")
    
    message_text = (
        f"<b>【{board_name}】</b>\n"
        f"投稿番号: #{post_id}\n"
        f"投稿日時: {posted_at}\n\n"
        f"{summary_text}"
    )
    
    keyboard = {
        "inline_keyboard": [[
            {"text": "🌐 ブラウザで詳細を確認", "url": target_post_url}
        ]]
    }
    
    send_msg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "HTML", # 太字を有効にする
        "reply_markup": json.dumps(keyboard)
    }
    
    try:
        resp = requests.post(send_msg_url, data=payload)
        if resp.status_code == 200:
            print(f"      [SUCCESS] 投稿#{post_id} の送信完了。")
        else:
            print(f"      [ERROR] Telegram送信失敗: {resp.text}")
    except Exception as e:
        print(f"      [ERROR] 通
