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
LOG_WITH_TITLE = False

# ===== 定数・環境変数 =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TARGET_URL = os.environ.get("TARGET_URL")
# GitHubへの書き込み権限のため必要
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN") 

# 必須チェック
missing = []
if not TELEGRAM_BOT_TOKEN: missing.append("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_CHAT_ID: missing.append("TELEGRAM_CHAT_ID")
if not TARGET_URL: missing.append("TARGET_URL")

if missing:
    print(f"Missing environment variables: {', '.join(missing)}")
    sys.exit(1)

url_list = [u.strip() for u in TARGET_URL.split(",") if u.strip()]

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

URL_PATTERN = re.compile(r"https?://[\w/:%#\$&\?\(\)~\.=\+\-]+", re.IGNORECASE)

# ===== 状態管理 (GitHub保存対応) =====
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

def save_and_commit_id(board_id: str, post_id: int):
    """IDを保存し、GitHubリポジトリにPushして記憶を永続化する"""
    fname = f"last_post_id_{board_id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(str(post_id))
    
    # GitHub Actions環境でのみ実行
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
            subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
            subprocess.run(["git", "add", fname], check=True)
            # 差分がある場合のみコミット
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", f"chore: update last_id {board_id} to {post_id}"], check=True)
                subprocess.run(["git", "push"], check=True)
                print(f"      [SYSTEM] 既読ID #{post_id} をリポジトリに保存しました。")
        except Exception as e:
            print(f"      [ERROR] Git保存失敗: {e}")

# ===== ユーティリティ =====
def extract_urls(text: str):
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    filtered_urls = []
    anchor_regex = re.compile(r'/[a-zA-Z0-9]+/\d+$')

    for url in unique_urls:
        # アンカーリンク除外の徹底
        if anchor_regex.search(url):
            if "disp" not in url and "upup.be" not in url:
                continue
        filtered_urls.append(url)
    return filtered_urls

# ===== Telegram送信ロジック =====
def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    valid_media_list = []
    
    for m_url in media_urls:
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        d_char = parsed.netloc.split('.')[0]
        
        if d_char.startswith("cdn"):
            base_netloc = parsed.netloc
        else:
            base_netloc = f"cdn{d_char}.5chan.jp" if len(d_char) == 1 else parsed.netloc

        # 動画判定を優先
        attempts = [
            {"type": "video", "url": f"https://{base_netloc}/file/{file_id}.mp4", "file_id": file_id},
            {"type": "photo", "url": f"https://{base_netloc}/file/plane/{file_id}.jpg", "file_id": file_id}
        ]
        
        for attempt in attempts:
            try:
                # 確実に判定するためstream=True
                r = requests.get(attempt["url"], headers=headers, stream=True, timeout=10)
                if r.status_code == 200:
                    valid_media_list.append(attempt)
                    break
            except: continue

    if not valid_media_list:
        return False

    summary_text = body_text[:300] + ("..." if len(body_text) > 300 else "")
    caption_text = f"<b>【{board_name}】</b>\n#{post_id} | {posted_at}\n\n{summary_text}"
    keyboard = {"inline_keyboard": [[{"text": "掲示板", "url": board_url}, {"text": "投稿", "url": target_post_url}]]}

    for media in valid_media_list:
        method = "sendVideo" if media["type"] == "video" else "sendPhoto"
        try:
            media_resp = requests.get(media["url"], headers=headers, timeout=20)
            media_file = io.BytesIO(media_resp.content)
            payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption_text, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
            files = {"video" if media["type"] == "video" else "photo": (f"media_{post_id}", media_file)}
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", data=payload, files=files)
        except: continue
    return True

# ===== メイン処理 =====
for target in url_list:
    board_id = get_board_id(target)
    try:
        resp = requests.get(target, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
    except: continue

    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    print(f"--- Checking board: {board_name} ---")
    
    articles = soup.select("article.resentry")
    last_post_id = load_last_post_id(board_id)
    newest_processed_id = last_post_id

    # 古い順にスキャンして未読分をすべてチェック
    for article in reversed(articles):
        try:
            post_id = int("".join(filter(str.isdigit, article.select_one("span.eno a").get_text())))
        except: continue

        if last_post_id is not None and post_id <= last_post_id:
            continue
            
        # 初回実行時は最新IDの記録のみ
        if last_post_id is None:
            newest_processed_id = max(newest_processed_id or 0, post_id)
            continue

        print(f"  -> [ANALYZING] 投稿#{post_id}")
        comment_div = article.select_one("div.comment")
        body_text = comment_div.get_text("\n", strip=True) if comment_div else ""
        
        media_urls = extract_urls(body_text)
        for a in article.select(".filethumblist li a[href]"):
            media_urls.append(urljoin(target, a.get("href")))

        # メディアがある場合のみ通知
        if media_urls:
            posted_at = article.select_one("time.date").get_text(strip=True) if article.select_one("time.date") else ""
            send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, target.split('?')[0]+"/", f"{target.split('?')[0]}/{post_id}", list(set(media_urls)))
        
        newest_processed_id = max(newest_processed_id or 0, post_id)

    # 最後に既読IDを更新してリポジトリに保存
    if newest_processed_id and newest_processed_id != last_post_id:
        save_and_commit_id(board_id, newest_processed_id)
