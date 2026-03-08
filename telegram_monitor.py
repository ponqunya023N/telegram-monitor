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
# EXTERNAL_DOMAINSには解析対象とするドメインをカンマ区切りで入れます
DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "") 
EXTERNAL_DOMAINS = [d.strip() for d in os.environ.get("EXTERNAL_DOMAINS", "").split(",") if d.strip()]
MEDIA_PREFIX = "cdn" 
# ----------------

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TARGET_URL]):
    print("Missing environment variables.")
    sys.exit(1)

url_list = [u.strip() for u in TARGET_URL.split(",") if u.strip()]
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
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

# ===== 通信ユーティリティ =====

def fetch_content_with_retry(url, timeout=30, retries=5):
    """ストリーミングとRangeヘッダーを利用して確実にダウンロードします"""
    content = bytearray()
    
    for i in range(retries):
        try:
            current_headers = headers.copy()
            if len(content) > 0:
                current_headers['Range'] = f"bytes={len(content)}-"
                
            with requests.get(url, headers=current_headers, timeout=timeout, stream=True) as res:
                if res.status_code in [200, 206]:
                    if res.status_code == 200:
                        content = bytearray()
                        
                    for chunk in res.iter_content(chunk_size=16384):
                        if chunk:
                            content.extend(chunk)
                    
                    expected_size = res.headers.get('Content-Length')
                    if expected_size and res.status_code == 200 and int(expected_size) != len(content):
                        raise requests.exceptions.ContentDecodingError("Incomplete download")
                    
                    return bytes(content)
                
                if res.status_code == 404:
                    return None
                    
                print(f"      [WARN] HTTP {res.status_code} for {url} (Attempt {i+1}/{retries})")
                
        except (requests.exceptions.RequestException, Exception) as e:
            wait_time = 2 ** (i + 1)
            print(f"      [WARN] Download error on {url}: {e} (Attempt {i+1}/{retries}). Retrying in {wait_time}s...")
            time.sleep(wait_time)
            
    return None

def fetch_page_soup(url, timeout=15):
    """HTMLを取得してBeautifulSoupオブジェクトを返します"""
    try:
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code == 200:
            return BeautifulSoup(res.text, "html.parser")
    except: pass
    return None

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
    """
    外部ページからメディアを抽出します。
    EXTERNAL_DOMAINSに含まれるドメインのみを解析対象とします。
    GIFはビデオ（Animation GIF）として扱い、画像の枠からは除外します。
    """
    if depth > 1:
        return None

    # URLがいずれかの許可ドメインを含んでいるかチェック
    is_target = any(domain in url for domain in EXTERNAL_DOMAINS if domain)
    
    if is_target:
        soup = fetch_page_soup(url)
        if soup:
            try:
                found_media_in_page = []

                # --- 特定ドメインの優先解析ロジック ---
                parsed_url = urlparse(url)
                domain = parsed_url.netloc

                # (1) 5chan (e.5chan.jp) 向けの最適化
                if "5chan.jp" in domain:
                    # 動画リンク（<a>タグの.mp4等）を優先。GIFも含める
                    v_link = soup.find("a", href=re.compile(r'\.(mp4|mov|wmv|webm|gif)', re.I))
                    if v_link:
                        v_url = urljoin(url, v_link["href"])
                        found_media_in_page.append({"type": "video", "url": v_url, "ext": v_url.split(".")[-1].split("?")[0].lower()})
                    
                    # メイン画像（class="MainImg"）を抽出
                    main_img = soup.find("img", class_="MainImg")
                    if main_img:
                        i_url = urljoin(url, main_img["src"])
                        m_obj = {"type": "photo", "url": i_url, "ext": i_url.split(".")[-1].split("?")[0].lower()}
                        if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)

                # (2) upup.be 向けの最適化
                elif "upup.be" in domain:
                    # alt属性に基づいて実ファイルへの親リンクを抽出
                    target_img = soup.find("img", alt=re.compile(r'(動画|画像)ファイル'))
                    if target_img and target_img.parent and target_img.parent.name == "a" and target_img.parent.get("href"):
                        m_url = urljoin(url, target_img.parent["href"])
                        # 拡張子を確認してタイプを決定
                        ext = m_url.split(".")[-1].split("?")[0].lower()
                        # GIFはビデオ扱いにする。またaltが「動画ファイル」の場合もビデオ
                        if ext == "gif" or "動画" in target_img.get("alt", ""):
                            m_type = "video"
                        else:
                            m_type = "photo"
                        found_media_in_page.append({"type": m_type, "url": m_url, "ext": ext})

                # --- 汎用解析ロジック（上記で見つからない場合や他ドメイン用） ---
                if not found_media_in_page:
                    # OGPメタタグ解析
                    og_video = soup.find("meta", property="og:video") or soup.find("meta", attrs={"name": "twitter:player:stream"})
                    if og_video and og_video.get("content"):
                        v_url = urljoin(url, og_video["content"])
                        ext = v_url.split(".")[-1].split("?")[0].lower()
                        found_media_in_page.append({"type": "video", "url": v_url, "ext": ext})

                    og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
                    if og_image and og_image.get("content"):
                        i_url = urljoin(url, og_image["content"])
                        ext = i_url.split(".")[-1].split("?")[0].lower()
                        # GIFは画像候補から除外
                        if ext != "gif":
                            found_media_in_page.append({"type": "photo", "url": i_url, "ext": ext})

                    # タグ解析
                    video_tag = soup.find("video")
                    if video_tag:
                        src = video_tag.get("src") or (video_tag.find("source").get("src") if video_tag.find("source") else None)
                        if src:
                            full_v_url = urljoin(url, src)
                            ext = full_v_url.split(".")[-1].split("?")[0].lower()
                            m_obj = {"type": "video", "url": full_v_url, "ext": ext}
                            if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)

                    for a in soup.find_all("a", href=True):
                        href_lower = a["href"].lower()
                        # ビデオ拡張子リストにgifを追加
                        if any(ext in href_lower for ext in [".mp4", ".mov", ".wmv", ".webm", ".gif"]):
                            full_v_url = urljoin(url, a["href"])
                            ext = full_v_url.split(".")[-1].split("?")[0].lower()
                            m_obj = {"type": "video", "url": full_v_url, "ext": ext}
                            if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)

                    for img in soup.find_all("img", src=True):
                        src = img.get("src")
                        if src and not any(x in src.lower() for x in ["qrcode", "logo", "icon", "titlemini"]):
                            full_img_url = urljoin(url, src)
                            ext = full_img_url.split(".")[-1].split("?")[0].lower()
                            # 画像拡張子リストからgifを削除
                            if ext in ["jpg", "jpeg", "png", "webp"]:
                                m_obj = {"type": "photo", "url": full_img_url, "ext": ext}
                                if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)

                if depth == 1 and found_media_in_page:
                    return found_media_in_page

                if depth == 0:
                    parsed_url = urlparse(url)
                    base_id = parsed_url.path.strip('/').split('/')[-1] 
                    child_links = []
                    for a in soup.find_all("a", href=True):
                        full_child_url = urljoin(url, a["href"])
                        if parsed_url.netloc in full_child_url and base_id in full_child_url and full_child_url != url:
                            child_links.append(full_child_url)
                    
                    found_media_list = found_media_in_page
                    for child_url in set(child_links):
                        child_results = resolve_external_media(child_url, depth=1)
                        if child_results:
                            for r in child_results:
                                if r not in found_media_list: found_media_list.append(r)
                    
                    return found_media_list
            except Exception as e:
                print(f"      [ERROR] BS4 analysis failed for {url}: {e}")
    return None

def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    """解析結果をTelegramへ送信します"""
    print(f"      [LOG] Analyzing media for #{post_id}...")
    valid_media_list = []
    
    for m_url in media_urls:
        external = resolve_external_media(m_url)
        if external:
            if isinstance(external, list): valid_media_list.extend(external)
            else: valid_media_list.append(external)
            continue

        parsed = urlparse(m_url)
        raw_file_id = parsed.path.rstrip("/").split("/")[-1]
        file_id = os.path.splitext(raw_file_id)[0] 
        
        netloc = parsed.netloc
        if DOMAIN_SUFFIX and DOMAIN_SUFFIX in netloc:
            if not netloc.startswith(MEDIA_PREFIX):
                subdomain = netloc.split('.')[0]
                netloc = f"{MEDIA_PREFIX}{subdomain}{DOMAIN_SUFFIX}"

        candidates = []
        # ビデオ候補。GIFは Animation GIF として sendVideo で送信可能
        for ext in ["mp4", "mov", "webm", "gif"]:
            candidates.append({"type": "video", "url": f"https://{netloc}/file/{file_id}.{ext}", "ext": ext})
        # 画像候補。GIFはAnimation GIFとしてsendVideoで送信するため、ここでは除外
        for ext in ["png", "jpg", "jpeg"]:
            candidates.append({"type": "photo", "url": f"https://{netloc}/file/plane/{file_id}.{ext}", "ext": ext})

        for attempt in candidates:
            content = fetch_content_with_retry(attempt["url"], timeout=10, retries=1)
            if content:
                attempt["content"] = content 
                valid_media_list.append(attempt)
                break

    unique_media = []
    seen_urls = set()
    for m in valid_media_list:
        if m["url"] not in seen_urls:
            unique_media.append(m)
            seen_urls.add(m["url"])

    caption = f"<b>【{board_name}】</b>\n#{post_id} | {posted_at}\n\n{body_text[:400]}"
    keyboard = {"inline_keyboard": [[{"text": "Site", "url": board_url}, {"text": "Original", "url": target_post_url}]]}

    if not unique_media:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
        )
        return

    for media in unique_media:
        #GIFはtype="video"になっているため、sendVideoで送信される（Animation GIF）
        method = "sendVideo" if media["type"] == "video" else "sendPhoto"
        file_content = media.get("content") or fetch_content_with_retry(media["url"], timeout=45, retries=5)
        
        if file_content:
            try:
                files = {("video" if media["type"] == "video" else "photo"): (f"file.{media['ext']}", file_content)}
                tel_res = requests.post(
                    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                    data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)},
                    files=files,
                    timeout=60
                )
                if tel_res.status_code != 200:
                    print(f"      [ERROR] Telegram API failed: {tel_res.text}")
            except Exception as e:
                print(f"      [ERROR] Exception during Telegram send: {e}")
        else:
            print(f"      [ERROR] All download attempts failed for: {media['url']}")

# ===== 処理実行ループ =====
for index, target in enumerate(url_list, start=1):
    board_id = get_board_id(target, index)

    soup = fetch_page_soup(target)
    if not soup:
        print(f" [ERROR] Target {board_id} failed to load.")
        continue

    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    print(f"--- Checking: {board_id} ---")
    
    articles = soup.select("article.resentry")
    
    saved_max_id, last_ids_list = load_last_post_ids_ab(board_id)
    new_max_id = saved_max_id
    current_batch_ids = []

    for article in reversed(articles):
        try:
            enotext = article.select_one("span.eno a").get_text(strip=True)
            post_id = int(re.search(r'\d+', enotext).group())
        except: continue
        
        if saved_max_id is not None and post_id <= saved_max_id:
            continue
        
        if post_id in last_ids_list:
            if saved_max_id is not None and post_id > saved_max_id: pass
            else: continue

        if post_id in sent_post_ids: continue
        
        if saved_max_id is None:
            current_batch_ids.append(post_id)
            new_max_id = max(new_max_id or 0, post_id)
            continue

        print(f"  -> [NEW] Item #{post_id} detected.")
        postedat = article.select_one("time.date").get_text(strip=True) if article.select_one("time.date") else "N/A"
        bodytext = article.select_one("div.comment").get_text("\n", strip=True) if article.select_one("div.comment") else ""
        
        media_urls = [urljoin(target, a["href"]) for a in article.select(".filethumblist li a[href]")]
        extracted = extract_urls(bodytext)
        
        if extracted or media_urls:
            media_urls.extend(extracted)
            send_telegram_combined(board_name, board_id, post_id, postedat, bodytext, target, f"{target}/{post_id}", list(set(media_urls)))
            sent_post_ids.add(post_id)
        
        current_batch_ids.append(post_id)
        new_max_id = max(new_max_id or 0, post_id)

    if current_batch_ids:
        save_last_post_ids_local_ab(board_id, new_max_id, current_batch_ids)
    else:
        print(" [LOG] No new items.")

commit_and_push_all()
