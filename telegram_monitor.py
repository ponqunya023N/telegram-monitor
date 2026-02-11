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

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TARGET_URL]):
    print("Missing environment variables.")
    sys.exit(1)

url_list = [u.strip() for u in TARGET_URL.split(",") if u.strip()]
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
sent_post_ids = set()
URL_PATTERN = re.compile(r"https?://[\w/:%#\$&\?\(\)~\.=\+\-]+", re.IGNORECASE)

# 更新があったファイルを記録するリスト
updated_files = []

# ===== 状態管理 =====
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
    except: return None

def save_last_post_id_local(board_id: str, post_id: int):
    """ローカルファイルのみ更新し、更新ファイルリストに追加"""
    fname = f"last_post_id_{board_id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(str(post_id))
    if fname not in updated_files:
        updated_files.append(fname)

def commit_and_push_all():
    """全処理の最後に1回だけまとめてプッシュ"""
    if not updated_files:
        print(" [LOG] 更新が必要なIDファイルはありません。")
        return
    
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
            subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
            for f in updated_files:
                subprocess.run(["git", "add", f], check=True)
            
            # 実際に差分がある場合のみプッシュ
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", "update multiple last_ids"], check=True)
                subprocess.run(["git", "push"], check=True)
                print(f" [LOG] {len(updated_files)}件のIDファイルをプッシュしました。")
        except Exception as e:
            print(f" [ERROR] Push failed: {e}")

# ===== 送信ロジック・ユーティリティ (略さず全文維持) =====
def extract_urls(text: str):
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    filtered_urls = []
    anchor_regex = re.compile(r'/[a-zA-Z0-9]+/\d+$')
    for url in unique_urls:
        if anchor_regex.search(url) and "disp" not in url and "upup.be" not in url: continue
        filtered_urls.append(url)
    return filtered_urls

def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    print(f"      [LOG] 投稿#{post_id} のメディア解析開始")
    valid_media_list = []
    for m_url in media_urls:
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        netloc = parsed.netloc if parsed.netloc.startswith("cdn") else f"cdn{parsed.netloc.split('.')[0]}.5chan.jp"
        for attempt in [
            {"type": "video", "url": f"https://{netloc}/file/{file_id}.mp4", "file_id": file_id},
            {"type": "photo", "url": f"https://{netloc}/file/plane/{file_id}.jpg", "file_id": file_id}
        ]:
            try:
                # stream=Trueを使用してヘッダーのみチェック
                if requests.get(attempt["url"], headers=headers, stream=True, timeout=10).status_code == 200:
                    valid_media_list.append(attempt)
                    break
            except: continue

    if not valid_media_list: return

    caption = f"<b>【{board_name}】</b>\n#{post_id} | {posted_at}\n\n{body_text[:300]}"
    keyboard = {"inline_keyboard": [[{"text": "掲示板", "url": board_url}, {"text": "投稿", "url": target_post_url}]]}

    for media in valid_media_list:
        method = "sendVideo" if media["type"] == "video" else "sendPhoto"
        try:
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)},
                files={("video" if media["type"] == "video" else "photo"): requests.get(media["url"], headers=headers).content}
            )
        except: pass

# ===== メインループ =====
for target in url_list:
    board_id = get_board_id(target)
    try:
        resp = requests.get(target, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
    except: continue

    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    print(f"--- Checking board: {board_id} ---")
    
    articles = soup.select("article.resentry")
    last_id = load_last_post_id(board_id)
    new_last_id = last_id

    for article in reversed(articles):
        try:
            eno_text = article.select_one("span.eno a").get_text(strip=True)
            post_id = int(re.search(r'\d+', eno_text).group())
        except: continue

        if last_id is not None and post_id <= last_id: continue
        if post_id in sent_post_ids: continue
        
        # 初回実行時はIDの更新のみ行う
        if last_id is None:
            new_last_id = max(new_last_id or 0, post_id)
            continue

        print(f"  -> [NEW] 投稿#{post_id} を検知しました。")
        posted_at = article.select_one("time.date").get_text(strip=True) if article.select_one("time.date") else "N/A"
        body_text = article.select_one("div.comment").get_text("\n", strip=True) if article.select_one("div.comment") else ""
        
        media_urls = [urljoin(target, a["href"]) for a in article.select(".filethumblist li a[href]")]
        for u in extract_urls(body_text):
            if "disp" in u or "upup.be" in u: media_urls.append(u)

        send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, target, f"{target}/{post_id}", list(set(media_urls)))
        sent_post_ids.add(post_id)
        new_last_id = max(new_last_id or 0, post_id)

    if new_last_id and new_last_id != last_id:
        save_last_post_id_local(board_id, new_last_id)
    else:
        print(" [LOG] 新着なし")

# 全てのボードの処理が終わったら、最後に1回だけプッシュ
commit_and_push_all()
