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
def send_telegram_media_group(board_id, post_id, posted_at, body_text, target_post_url, media_urls):
    """
    メディアをグループ（アルバム）として送信し、
    その直後に詳細情報と「ブラウザで開く」ボタンを送信する。
    """
    print(f"     [DEBUG] Telegramへ送信を試みます... (Media: {len(media_urls)})")
    
    # 1. メディアの準備 (最大10枚)
    media_group = []
    processed_count = 0
    
    for m_url in media_urls:
        if processed_count >= 10: break
        
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        d_char = parsed.netloc.split('.')[0]
        base_netloc = base_netloc = parsed.netloc if d_char.startswith("cdn") else f"cdn{d_char}.5chan.jp"

        # 試行URLリスト（画像優先 -> 動画）
        attempt_urls = [
            f"https://{base_netloc}/file/plane/{file_id}.jpg",
            f"https://{base_netloc}/file/{file_id}.mp4",
            f"https://{base_netloc}/file/plane/{file_id}.png",
            f"https://{base_netloc}/file/{file_id}.gif"
        ]
        if "." in file_id: attempt_urls.insert(0, m_url)

        for target_download_url in attempt_urls:
            # Telegram APIはURLを直接受け取れるため、存否確認のみ行う
            try:
                r = requests.head(target_download_url, headers=headers, timeout=10)
                if r.status_code == 200:
                    ext = target_download_url.split('.')[-1].lower()
                    media_type = "video" if ext in ["mp4", "mov", "webm"] else "photo"
                    media_group.append({"type": media_type, "media": target_download_url})
                    processed_count += 1
                    break
            except: continue

    # 2. メディアグループの送信
    if media_group:
        send_group_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
        requests.post(send_group_url, data={"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media_group)})

    # 3. テキストとインラインボタンの送信
    message_text = (
        f"【新着投稿: {board_id}】\n"
        f"No: {post_id}\n"
        f"日時: {posted_at}\n\n"
        f"{body_text[:500]}" # 長すぎる場合はカット
    )
    
    keyboard = {
        "inline_keyboard": [[
            {"text": "🌐 ブラウザで開く", "url": target_post_url}
        ]]
    }
    
    send_msg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "reply_markup": json.dumps(keyboard)
    }
    
    try:
        resp = requests.post(send_msg_url, data=payload)
        if resp.status_code == 200:
            print(f"     [SUCCESS] 投稿#{post_id} の送信完了。")
        else:
            print(f"     [ERROR] Telegram送信失敗: {resp.text}")
    except Exception as e:
        print(f"     [ERROR] 通信エラー: {e}")

# ===== メイン処理 =====
for target in url_list:
    board_id = get_board_id(target)
    print(f"--- Checking board: {board_id} ---")
    try:
        resp = requests.get(target, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f" [ERROR] ボード読み込み失敗 ({target}): {e}")
        continue

    soup = BeautifulSoup(resp.text, "html.parser")
    articles = soup.select("article.resentry")
    
    if not articles: continue

    # 最新の1件のみを対象とする
    target_articles = articles[-1:]
    last_post_id = load_last_post_id(board_id)
    newest_post_id = None
    
    for article in reversed(target_articles):
        eno_tag = article.select_one("span.eno a")
        if eno_tag is None: continue 
        try:
            post_id = int("".join(filter(str.isdigit, eno_tag.get_text(strip=True))))
        except: continue

        if newest_post_id is None or post_id > newest_post_id: newest_post_id = post_id
        
        if post_id in sent_post_ids: continue
        if last_post_id is not None and post_id <= last_post_id:
            print(f"  -> 投稿#{post_id} (既読のためスキップ)")
            continue
        
        print(f"  -> [NEW] 投稿#{post_id} を処理中...")
        time_tag = article.select_one("time.date")
        posted_at = time_tag.get_text(strip=True) if time_tag else "N/A"
        comment_div = article.select_one("div.comment")
        body_text = comment_div.get_text("\n", strip=True) if comment_div else ""

        media_urls = []
        
        # 本文からの抽出
        urls_in_body = extract_urls(body_text)
        for u in urls_in_body:
            if "disp" in u or "upup.be" in u: media_urls.append(u)

        # サムネイルリストからの抽出
        thumblist = article.select(".filethumblist li")
        for li in thumblist:
            a_tag = li.select_one("a[href]")
            if a_tag:
                abs_url = urljoin(target, a_tag.get("href"))
                media_urls.append(abs_url)

        if not media_urls:
            print(f"  -> 投稿#{post_id} は画像/動画がないためスキップ。")
            continue

        target_post_url = f"{target.rstrip('/')}/{post_id}"
        send_telegram_media_group(
            board_id, post_id, posted_at, body_text, 
            target_post_url, list(dict.fromkeys(media_urls))
        )
        sent_post_ids.add(post_id)

    if newest_post_id is not None:
        save_last_post_id(board_id, newest_post_id)
