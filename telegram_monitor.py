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
    
    # GitHubリポジトリへの保存
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
            subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
            subprocess.run(["git", "add", fname], check=True)
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", f"update last_id {board_id}"], check=True)
                subprocess.run(["git", "push"], check=True)
        except Exception:
            pass

# ===== ユーティリティ =====
def extract_urls(text: str):
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    filtered_urls = []
    anchor_regex = re.compile(r'/[a-zA-Z0-9]+/\d+$')

    for url in unique_urls:
        if anchor_regex.search(url):
            if "disp" not in url and "upup.be" not in url:
                continue
        if "disp" in url or "upup.be" in url:
            filtered_urls.append(url)
            continue
        filtered_urls.append(url)
    return filtered_urls

# ===== Telegram送信ロジック =====
def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    print(f"      [LOG] 投稿#{post_id} のメディア解析開始 (候補数: {len(media_urls)})")
    valid_media_list = []
    
    for m_url in media_urls:
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        netloc_parts = parsed.netloc.split('.')
        d_char = netloc_parts[0]
        
        if d_char.startswith("cdn"):
            base_netloc = parsed.netloc
        else:
            base_netloc = f"cdn{d_char}.5chan.jp" if len(d_char) == 1 else parsed.netloc

        attempts = [
            {"type": "video", "url": f"https://{base_netloc}/file/{file_id}.mp4", "file_id": file_id},
            {"type": "photo", "url": f"https://{base_netloc}/file/plane/{file_id}.jpg", "file_id": file_id},
            {"type": "photo", "url": f"https://{base_netloc}/file/plane/{file_id}.png", "file_id": file_id}
        ]
        
        found_for_this_url = False
        for attempt in attempts:
            try:
                r = requests.get(attempt["url"], headers=headers, stream=True, timeout=10)
                if r.status_code == 200:
                    valid_media_list.append(attempt)
                    print(f"      [LOG] メディア特定成功: {attempt['type']} -> {attempt['url']}")
                    found_for_this_url = True
                    break
            except Exception:
                continue
        
        if not found_for_this_url:
            print(f"      [LOG] メディア特定失敗: {m_url}")

    if not valid_media_list:
        print(f"      [LOG] 有効メディアなし。この投稿の通知をスキップします。")
        return

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

    for media in valid_media_list:
        method = "sendVideo" if media["type"] == "video" else "sendPhoto"
        media_field = "video" if media["type"] == "video" else "photo"
        prefix = "[mov]" if media["type"] == "video" else "[pic]"
        suffix = ".mp4" if media["type"] == "video" else ".jpg"
        fname = f"{prefix}{media['file_id']}{suffix}"

        try:
            print(f"      [LOG] Telegram送信中: {fname} ({method})")
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
            
            res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}", data=payload, files=files)
            if res.status_code != 200:
                print(f"      [ERROR] Telegram APIエラー: {res.status_code} {res.text}")
        except Exception as e:
            print(f"      [ERROR] 送信プロセス失敗 ({fname}): {e}")

# ===== メイン処理 =====
for target in url_list:
    board_id = get_board_id(target)
    try:
        resp = requests.get(target, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"--- Checking board: {board_id} ---")
        print(f" [ERROR] 掲示板接続失敗 ({target}): {e}")
        continue

    soup = BeautifulSoup(resp.text, "html.parser")
    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    
    if LOG_WITH_TITLE:
        print(f"--- Checking board: {board_name} ({board_id}) ---")
    else:
        print(f"--- Checking board: {board_id} ---")
    
    articles = soup.select("article.resentry")
    if not articles:
        print(f" [LOG] 記事が見つかりません。")
        continue

    last_post_id = load_last_post_id(board_id)
    newest_processed_id = last_post_id

    # 記事を古い順に処理し、last_post_id より大きいものを全て検知する元のロジック
    for article in reversed(articles):
        eno_tag = article.select_one("span.eno a")
        if eno_tag is None: continue 
        try:
            post_id = int("".join(filter(str.isdigit, eno_tag.get_text(strip=True))))
        except Exception: continue

        # 最後に送ったID以下のものはスキップ（これが正しい差分検知）
        if last_post_id is not None and post_id <= last_post_id:
            continue
        
        if post_id in sent_post_ids:
            continue
            
        if last_post_id is None:
            newest_processed_id = max(newest_processed_id or 0, post_id)
            continue

        print(f"  -> [NEW] 投稿#{post_id} を検知しました。")
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
        newest_processed_id = max(newest_processed_id or 0, post_id)

    if newest_processed_id is not None and newest_processed_id != last_post_id:
        save_last_post_id(board_id, newest_processed_id)
    else:
        print(f" [LOG] 新着投稿はありませんでした。")
