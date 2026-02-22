import os
import sys
import re
import time
import json
import io
import subprocess
import hashlib # IDをハッシュ化するための標準ライブラリ
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ===== 設定スイッチ =====
LOG_WITH_TITLE = False 

# ===== 定数・環境変数 =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TARGET_URL = os.environ.get("TARGET_URL")

# --- 秘匿設定 ---
DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "") 
EXTERNAL_DOMAINS = os.environ.get("EXTERNAL_DOMAINS", "").split(",") 
MEDIA_PREFIX = "cdn" 
# ----------------

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
def get_board_id(url: str, index: int) -> str:
    """URLを識別用の符号に変換します"""
    hashed = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
    return f"{index:02d}_{hashed}"

def load_last_post_ids_ab(board_id: str):
    """保存されたID情報を読み込みます"""
    fname = f"last_post_id_{board_id}.txt"
    if not os.path.exists(fname): return None, []
    try:
        with open(fname, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
            if not lines: return None, []
            
            # 1行目：最大番号
            max_id = int(lines[0]) if lines[0].strip().isdigit() else None
            
            # 2行目：通知済みリスト
            id_list = []
            if len(lines) > 1:
                id_list = [int(x) for x in lines[1].split(",") if x.strip().isdigit()]
            return max_id, id_list
    except: return None, []

def save_last_post_ids_local_ab(board_id: str, max_id: int, post_ids: list):
    """最新のID情報を保存します"""
    fname = f"last_post_id_{board_id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"{max_id}\n")
        f.write(",".join(map(str, sorted(post_ids))))
    if fname not in updated_files:
        updated_files.append(fname)

def commit_and_push_all():
    """IDファイルの更新を確定させます"""
    if not updated_files:
        print(" [LOG] No ID files to update.")
        return
    
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
            subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
            for f in updated_files:
                subprocess.run(["git", "add", f], check=True)
            
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", "update files"], check=True)
                subprocess.run(["git", "pull", "--rebase"], check=False)
                subprocess.run(["git", "push"], check=True)
                print(f" [LOG] Pushed {len(updated_files)} files.")
        except Exception as e:
            print(f" [ERROR] Push failed: {e}")

# ===== 解析・送信ロジック =====
def extract_urls(text: str):
    """テキストからURLを抽出します"""
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    filtered_urls = []
    
    for url in unique_urls:
        if "/read.cgi/" in url:
            continue
        filtered_urls.append(url)
    return filtered_urls

def resolve_external_media(url, depth=0):
    """外部ページからメディアを抽出します（関連する子ページも含めて探索します）"""
    is_target = any(domain in url for domain in EXTERNAL_DOMAINS if domain)
    results = [] # 複数の動画が見つかる可能性があるため、リスト形式に変更
    
    if is_target and depth < 2: # 探索範囲を1階層のみに制限（無限ループ防止）
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                
                # 1. 現在のページに直接配置されている動画を探す
                video_tag = soup.find("video")
                if video_tag:
                    src = video_tag.get("src") or (video_tag.find("source").get("src") if video_tag.find("source") else None)
                    if src:
                        full_url = urljoin(url, src)
                        ext = full_url.split(".")[-1].split("?")[0]
                        results.append({"type": "video", "url": full_url, "ext": ext})
                        
                for a in soup.find_all("a", href=True):
                    if ".mov" in a["href"].lower() or ".mp4" in a["href"].lower():
                        full_url = urljoin(url, a["href"])
                        ext = full_url.split(".")[-1].split("?")[0]
                        results.append({"type": "video", "url": full_url, "ext": ext})
                        
                # 2. 現在のページから、関連する別の子ページへのリンクを探す（最初の親ページの時のみ）
                if depth == 0:
                    # URLの末尾のIDを取得（例: .../ID -> ID）
                    parent_id = urlparse(url).path.rstrip("/").split("/")[-1]
                    
                    for a in soup.find_all("a", href=True):
                        child_url = urljoin(url, a["href"])
                        is_child_target = any(domain in child_url for domain in EXTERNAL_DOMAINS if domain)
                        
                        # 同じ外部ドメインかつ、親のIDがURLに含まれている別ページであれば子ページとみなして探索
                        if is_child_target and child_url != url and parent_id and parent_id in child_url:
                            child_results = resolve_external_media(child_url, depth=1)
                            if child_results:
                                results.extend(child_results)
        except: pass
        
    # 重複排除処理（同じ動画URLが複数回登録されるのを防ぐため）
    unique_results = []
    seen_urls = set()
    for r in results:
        if r["url"] not in seen_urls:
            unique_results.append(r)
            seen_urls.add(r["url"])
            
    return unique_results

def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    """解析結果をTelegramへ送信します"""
    print(f"      [LOG] Analyzing media for #{post_id}...")
    valid_media_list = []
    
    for m_url in media_urls:
        external_results = resolve_external_media(m_url) # リストで返ってくるように変更
        if external_results:
            valid_media_list.extend(external_results) # 複数の動画結果を結合
            continue

        parsed = urlparse(m_url)
        raw_file_id = parsed.path.rstrip("/").split("/")[-1]
        file_id = os.path.splitext(raw_file_id)[0] 
        
        netloc = parsed.netloc
        if DOMAIN_SUFFIX and DOMAIN_SUFFIX in netloc:
            subdomain = netloc.split('.')[0]
            netloc = f"{MEDIA_PREFIX}{subdomain}{DOMAIN_SUFFIX}"

        candidates = []
        for ext in ["mp4", "mpg", "mov", "webm", "gif", "wmv"]:
            candidates.append({"type": "video", "url": f"https://{netloc}/file/{file_id}.{ext}", "ext": ext})
        for ext in ["jpg", "jpeg", "png", "bmp"]:
            candidates.append({"type": "photo", "url": f"https://{netloc}/file/plane/{file_id}.{ext}", "ext": ext})

        for attempt in candidates:
            try:
                if requests.get(attempt["url"], headers=headers, stream=True, timeout=5).status_code == 200:
                    valid_media_list.append(attempt)
                    break
            except: continue

    caption = f"<b>【{board_name}】</b>\n#{post_id} | {posted_at}\n\n{body_text[:400]}"
    keyboard = {"inline_keyboard": [[{"text": "Site", "url": board_url}, {"text": "Original", "url": target_post_url}]]}

    if not valid_media_list:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
        )
        return

    for media in valid_media_list:
        method = "sendVideo" if media["type"] == "video" else "sendPhoto"
        try:
            file_res = requests.get(media["url"], headers=headers, timeout=20)
            if file_res.status_code == 200:
                files = {("video" if media["type"] == "video" else "photo"): (f"file.{media['ext']}", file_res.content)}
                requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                    data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)},
                    files=files
                )
        except: pass

# ===== 処理実行ループ =====
for index, target in enumerate(url_list, start=1):
    board_id = get_board_id(target, index)

    try:
        resp = requests.get(target, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as e: 
        print(f" [ERROR] Target {board_id} failed. (URL hidden)")
        continue

    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    print(f"--- Checking: {board_id} ---")
    
    articles = soup.select("article.resentry")
    
    saved_max_id, last_ids_list = load_last_post_ids_ab(board_id)
    new_max_id = saved_max_id
    current_batch_ids = []

    for article in reversed(articles):
        try:
            eno_text = article.select_one("span.eno a").get_text(strip=True)
            post_id = int(re.search(r'\d+', eno_text).group())
        except: continue
        
        if saved_max_id is not None and post_id <= saved_max_id:
            continue
        
        if post_id in last_ids_list:
            if saved_max_id is not None and post_id > saved_max_id:
                pass
            else:
                continue

        if post_id in sent_post_ids: continue
        
        if saved_max_id is None:
            current_batch_ids.append(post_id)
            new_max_id = max(new_max_id or 0, post_id)
            continue

        print(f"  -> [NEW] Item #{post_id} detected.")
        posted_at = article.select_one("time.date").get_text(strip=True) if article.select_one("time.date") else "N/A"
        body_text = article.select_one("div.comment").get_text("\n", strip=True) if article.select_one("div.comment") else ""
        
        media_urls = [urljoin(target, a["href"]) for a in article.select(".filethumblist li a[href]")]
        extracted = extract_urls(body_text)
        
        if extracted or media_urls:
            media_urls.extend(extracted)
            send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, target, f"{target}/{post_id}", list(set(media_urls)))
            sent_post_ids.add(post_id)
        
        current_batch_ids.append(post_id)
        new_max_id = max(new_max_id or 0, post_id)

    if current_batch_ids:
        save_last_post_ids_local_ab(board_id, new_max_id, current_batch_ids)
    else:
        print(" [LOG] No new items.")

commit_and_push_all()
