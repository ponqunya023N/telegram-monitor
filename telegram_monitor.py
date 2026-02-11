import os
import sys
import re
import time
import json
import io
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
def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    """
    メディアごとに本文とボタンを統合して送信する。
    """
    valid_media_list = []
    
    for m_url in media_urls:
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        d_char = parsed.netloc.split('.')[0]
        base_netloc = parsed.netloc if d_char.startswith("cdn") else f"cdn{d_char}.5chan.jp"

        attempts = [
            {"type": "video", "url": f"https://{base_netloc}/file/{file_id}.mp4", "file_id": file_id},
            {"type": "photo", "url": f"https://{base_netloc}/file/plane/{file_id}.jpg", "file_id": file_id},
            {"type": "photo", "url": f"https://{base_netloc}/file/plane/{file_id}.png", "file_id": file_id}
        ]
        
        for attempt in attempts:
            try:
                r = requests.head(attempt["url"], headers=headers, timeout=10)
                if r.status_code == 200:
                    valid_media_list.append(attempt)
                    break
            except: continue

    summary_text = body_text[:300] + ("..." if len(body_text) > 300 else "")
    caption_text = (
        f"<b>【{board_name}】</b>\n"
        f"投稿番号: #{post_id}\n"
        f"投稿日時: {posted_at}\n\n"
        f"{summary_text}"
    )
    
    keyboard = {
        "inline_keyboard": [[
            {"text": "掲示板", "url": board_url},
            {"text": "投稿", "url": target_post_url}
        ]]
    }

    if not valid_media_list:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": caption_text,
            "parse_mode": "HTML",
            "reply_markup": json.dumps(keyboard)
        }
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", data=payload)
        return

    for media in valid_media_list:
        method = "sendVideo" if media["type"] == "video" else "sendPhoto"
        media_field = "video" if media["type"] == "video" else "photo"
        prefix = "[mov]" if media["type"] == "video" else "[pic]"
        suffix = ".mp4" if media["type"] == "video" else ".jpg"
        fname = f"{prefix}{media['file_id']}{suffix}"

        try:
            media_resp = requests.get(media["url"], headers=headers, timeout=20)
            media_resp.raise_for_status()
            media_file = io.BytesIO(media_resp.content)
            
            payload = {
                "chat_id": TELEGRAM_CHAT_ID,
                "caption": caption_text,
                "parse_mode": "HTML",
                "reply_markup": json.dumps(keyboard)
            }
            files = {media_field: (fname, media_file)}
            
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", data=payload, files=files)
        except Exception as e:
            print(f"      [ERROR] 送信失敗 ({fname}): {e}")

# ===== メイン処理 =====
for target in url_list:
    board_id = get_board_id(target)
    print(f"--- Checking board: {board_id} ---")
    try:
        resp = requests.get(target, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f" [ERROR] 接続失敗 ({target}): {e}")
        continue

    soup = BeautifulSoup(resp.text, "html.parser")
    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    
    articles = soup.select("article.resentry")
    if not articles: continue

    last_post_id = load_last_post_id(board_id)
    newest_post_id = last_post_id if last_post_id else 0
    
    # 既読ファイルがない場合（初回実行）のフラグ
    is_initial_run = (last_post_id is None)

    # 古い順に処理
    for article in reversed(articles):
        eno_tag = article.select_one("span.eno a")
        if eno_tag is None: continue 
        try:
            post_id = int("".join(filter(str.isdigit, eno_tag.get_text(strip=True))))
        except: continue

        # 最新IDを更新
        if post_id > newest_post_id:
            newest_post_id = post_id

        # 既読チェック
        if last_post_id is not None and post_id <= last_post_id:
            continue
        if post_id in sent_post_ids:
            continue
            
        # 初回実行時は、通知を送らずにループを継続（最新IDの取得のみ行う）
        if is_initial_run:
            continue

        print(f"  -> [NEW] 投稿#{post_id} を処理中...")
        time_tag = article.select_one("time.date")
        posted_at = time_tag.get_text(strip=True) if time_tag else "N/A"
        comment_div = article.select_one("div.comment")
        body_text = comment_div.get_text("\n", strip=True) if comment_div else ""

        media_urls = []
        urls_in_body = extract_urls(body_text)
        for u in urls_in_body:
            if "disp" in u or "upup.be" in u: media_urls.append(u)

        thumblist = article.select(".filethumblist li")
        for li in thumblist:
            a_tag = li.select_one("a[href]")
            if a_tag:
                abs_url = urljoin(target, a_tag.get("href"))
                media_urls.append(abs_url)

        base_target = target.split('?')[0].rstrip('/')
        send_telegram_combined(
            board_name, board_id, post_id, posted_at, body_text, 
            base_target + "/", f"{base_target}/{post_id}", list(dict.fromkeys(media_urls))
        )
        sent_post_ids.add(post_id)

    # 最後に既読IDを保存（重要：git commitの対象になります）
    if newest_post_id > (last_post_id or 0):
        if is_initial_run:
            print(f"  [INFO] 初回実行のため、最新ID #{newest_post_id} を記録して初期化完了。")
        save_last_post_id(board_id, newest_post_id)
