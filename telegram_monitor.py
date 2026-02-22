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

def resolve_external_media(url):
    """外部ページからメディアを抽出します（子ページも含め、見つかったすべての動画を返します）"""
    is_target = any(domain in url for domain in EXTERNAL_DOMAINS if domain)
    results = []
    
    if is_target:
        # URLから基本となるID（例：KKXCQ7Xa）を抽出
        url_id = urlparse(url).path.strip("/").split("/")[-1]
        visited_urls = set([url])

        # ページ内から動画情報を抜き出す補助関数
        def extract_from_soup(soup, base_url):
            found_media = []
            # <video>タグを探す
            video_tag = soup.find("video")
            if video_tag:
                src = video_tag.get("src") or (video_tag.find("source").get("src") if video_tag.find("source") else None)
                if src:
                    full_url = urljoin(base_url, src)
                    ext = full_url.split(".")[-1].split("?")[0]
                    found_media.append({"type": "video", "url": full_url, "ext": ext})
            # 直接.mp4等へのリンクがある場合も探す
            for a in soup.find_all("a", href=True):
                href = a["href"].lower()
                if ".mov" in href or ".mp4" in href or ".mpg" in href or ".webm" in href:
                    full_url = urljoin(base_url, a["href"])
                    ext = full_url.split(".")[-1].split("?")[0]
                    found_media.append({"type": "video", "url": full_url, "ext": ext})
            return found_media

        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                
                # 1. 最初のページから動画を探す
                results.extend(extract_from_soup(soup, url))
                
                # 2. ページ内にある「子ページ」へのリンクを探して巡回する
                base_domain = urlparse(url).netloc
                for a in soup.find_all("a", href=True):
                    full_link = urljoin(url, a["href"])
                    parsed_link = urlparse(full_link)
                    
                    # 同ドメインかつ未訪問で、かつ元のURLのIDが含まれているリンクを対象にする
                    if parsed_link.netloc == base_domain and full_link not in visited_urls:
                        # リンク自体が動画ファイルでないこと（ページであることを期待）
                        if not any(full_link.lower().endswith(ext) for ext in [".mp4", ".mov", ".webm"]):
                            # URL全体（クエリパラメータ含む）のどこかにIDがあればOKとする
                            if url_id and url_id in full_link:
                                visited_urls.add(full_link)
                                try:
                                    child_res = requests.get(full_link, headers=headers, timeout=10)
                                    if child_res.status_code == 200:
                                        child_soup = BeautifulSoup(child_res.text, "html.parser")
                                        results.extend(extract_from_soup(child_soup, full_link))
                                except:
                                    pass
        except:
            pass
            
    # 重複する動画URLを除去
    unique_results = []
    seen_urls = set()
    for item in results:
        if item["url"] not in seen_urls:
            unique_results.append(item)
            seen_urls.add(item["url"])
            
    return unique_results

def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    """解析結果をTelegramへ送信します"""
    print(f"      [LOG] Analyzing media for #{post_id}...")
    valid_media_list = []
    
    for m_url in media_urls:
        # 子ページも含めて動画を探す
        externals = resolve_external_media(m_url)
        if externals:
            valid_media_list.extend(externals)
            continue

        # 以下、従来のメディア解析ロジック（変更なし）
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

    # 見つかったすべてのメディア（動画等）を送信
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
