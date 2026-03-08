import os
import sys
import re
import time
import json
import io
import subprocess
import hashlib 
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# ===== 設定スイッチ =====
LOG_WITH_TITLE = False 

# ===== 定数・環境変数 (Secretsより取得) =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TARGET_URL = os.environ.get("TARGET_URL")

# --- 秘匿設定の反映 ---
DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "") 
EXTERNAL_DOMAINS = [d.strip() for d in os.environ.get("EXTERNAL_DOMAINS", "").split(",") if d.strip()]
MEDIA_PREFIX = "cdn" 
# ----------------

if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TARGET_URL]):
    print("Missing critical environment variables.")
    sys.exit(1)

url_list = [u.strip() for u in TARGET_URL.split(",") if u.strip()]
headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
sent_entry_ids = set()
URL_PATTERN = re.compile(r"https?://[\w/:%#\$&\?\(\)~\.=\+\-]+", re.IGNORECASE)

updated_files = []

# ===== 状態管理 =====
def get_identifier(url: str, index: int) -> str:
    """URLを識別符号に変換"""
    hashed = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
    return f"{index:02d}_{hashed}"

def load_processed_ids(target_id: str):
    """保存済みの進行情報を読み込み"""
    fname = f"last_post_id_{target_id}.txt"
    if not os.path.exists(fname): return None, []
    try:
        with open(fname, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
            if not lines: return None, []
            
            max_id = int(lines[0]) if lines[0].strip().isdigit() else None
            id_list = []
            if len(lines) > 1:
                id_list = [int(x) for x in lines[1].split(",") if x.strip().isdigit()]
            return max_id, id_list
    except: return None, []

def save_processed_ids(target_id: str, max_id: int, entry_ids: list):
    """最新の進行情報を保存"""
    fname = f"last_post_id_{target_id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"{max_id}\n")
        f.write(",".join(map(str, sorted(entry_ids))))
    if fname not in updated_files:
        updated_files.append(fname)

def sync_repository():
    """リポジトリの状態を確定"""
    if not updated_files:
        return
    
    if os.environ.get("GITHUB_ACTIONS") == "true":
        try:
            subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
            subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
            for f in updated_files:
                subprocess.run(["git", "add", f], check=True)
            
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", "update state"], check=True)
                subprocess.run(["git", "pull", "--rebase"], check=False)
                subprocess.run(["git", "push"], check=True)
        except Exception as e:
            print(f" [ERROR] Sync failed: {e}")

# ===== 通信ユーティリティ =====

def download_media(url, timeout=30, retries=5):
    """メディアファイルを確実に取得"""
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
                        if chunk: content.extend(chunk)
                    return bytes(content)
                if res.status_code == 404: return None
        except:
            time.sleep(2 ** (i + 1))
    return None

def get_soup(url, timeout=15):
    """HTML解析オブジェクトを取得"""
    try:
        res = requests.get(url, headers=headers, timeout=timeout)
        if res.status_code == 200:
            return BeautifulSoup(res.text, "html.parser")
    except: pass
    return None

# ===== 解析・送信ロジック =====
def parse_text_urls(text: str):
    """本文からURLを抽出"""
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    return [u for u in unique_urls if "/read.cgi/" not in u]

def resolve_media_from_page(url, depth=0):
    """
    外部URLからメディアを解析。
    環境変数の許可ドメインに基づき、構造的特徴から抽出を行う。
    """
    if depth > 1: return None
    if not any(domain in url for domain in EXTERNAL_DOMAINS if domain): return None

    soup = get_soup(url)
    if not soup: return None

    try:
        found_media = []
        parsed_url = urlparse(url)

        # --- 構造パターンA: 特定のメディアクラスを優先 ---
        if soup.find(class_="MainImg"):
            # アニメーションGIFを含むビデオ候補
            v_link = soup.find("a", href=re.compile(r'\.(mp4|mov|wmv|webm|gif)', re.I))
            if v_link:
                v_url = urljoin(url, v_link["href"])
                found_media.append({"type": "video", "url": v_url, "ext": v_url.split(".")[-1].split("?")[0].lower()})
            
            # 画像（GIFは除外）
            main_img = soup.find("img", class_="MainImg")
            if main_img:
                i_url = urljoin(url, main_img["src"])
                ext = i_url.split(".")[-1].split("?")[0].lower()
                if ext != "gif":
                    found_media.append({"type": "photo", "url": i_url, "ext": ext})

        # --- 構造パターンB: alt属性による分類 ---
        target_img = soup.find("img", alt=re.compile(r'(動画|画像)ファイル'))
        if target_img and target_img.parent and target_img.parent.name == "a":
            m_url = urljoin(url, target_img.parent.get("href", ""))
            ext = m_url.split(".")[-1].split("?")[0].lower()
            # GIFまたは明示的な動画指定はビデオ扱い
            m_type = "video" if (ext == "gif" or "動画" in target_img.get("alt", "")) else "photo"
            if m_type == "photo" and ext == "gif": pass # 念のため除外
            else: found_media.append({"type": m_type, "url": m_url, "ext": ext})

        # --- 汎用解析 (上記で見つからない場合) ---
        if not found_media:
            # OGP解析
            meta_v = soup.find("meta", property="og:video") or soup.find("meta", attrs={"name": "twitter:player:stream"})
            if meta_v and meta_v.get("content"):
                v_url = urljoin(url, meta_v["content"])
                found_media.append({"type": "video", "url": v_url, "ext": v_url.split(".")[-1].split("?")[0].lower()})

            meta_i = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
            if meta_i and meta_i.get("content"):
                i_url = urljoin(url, meta_i["content"])
                ext = i_url.split(".")[-1].split("?")[0].lower()
                if ext != "gif": found_media.append({"type": "photo", "url": i_url, "ext": ext})

            # タグ解析
            for a in soup.find_all("a", href=True):
                h = a["href"].lower()
                if any(ex in h for ex in [".mp4", ".mov", ".wmv", ".webm", ".gif"]):
                    v_url = urljoin(url, a["href"])
                    found_media.append({"type": "video", "url": v_url, "ext": v_url.split(".")[-1].split("?")[0].lower()})

            for img in soup.find_all("img", src=True):
                s = img.get("src")
                if s and not any(x in s.lower() for x in ["qrcode", "logo", "icon", "titlemini"]):
                    i_url = urljoin(url, s)
                    ext = i_url.split(".")[-1].split("?")[0].lower()
                    if ext in ["jpg", "jpeg", "png", "webp"]: # GIFは除外
                        found_media.append({"type": "photo", "url": i_url, "ext": ext})

        if depth == 1: return found_media

        if depth == 0:
            # 同一階層のリンクを再帰探索
            base_id = parsed_url.path.strip('/').split('/')[-1]
            for a in soup.find_all("a", href=True):
                child_url = urljoin(url, a["href"])
                if parsed_url.netloc in child_url and base_id in child_url and child_url != url:
                    results = resolve_media_from_page(child_url, depth=1)
                    if results: found_media.extend(results)
            
            # 重複排除
            unique_results = []
            seen = set()
            for m in found_media:
                if m["url"] not in seen:
                    unique_results.append(m)
                    seen.add(m["url"])
            return unique_results

    except: pass
    return None

def process_and_notify(site_name, target_id, entry_id, ts, text, site_url, entry_url, media_links):
    """解析と通知の実行"""
    results = []
    for link in media_links:
        resolved = resolve_media_from_page(link)
        if resolved:
            results.extend(resolved if isinstance(resolved, list) else [resolved])
            continue

        # 環境変数の接尾辞を利用した直接解決
        parsed = urlparse(link)
        f_id = os.path.splitext(parsed.path.split("/")[-1])[0]
        netloc = parsed.netloc
        if DOMAIN_SUFFIX and DOMAIN_SUFFIX in netloc:
            if not netloc.startswith(MEDIA_PREFIX):
                netloc = f"{MEDIA_PREFIX}{netloc.split('.')[0]}{DOMAIN_SUFFIX}"

        # GIFはビデオ扱い
        for ex in ["mp4", "mov", "webm", "gif"]:
            results.append({"type": "video", "url": f"https://{netloc}/file/{f_id}.{ex}", "ext": ex})
        for ex in ["png", "jpg", "jpeg"]:
            results.append({"type": "photo", "url": f"https://{netloc}/file/plane/{f_id}.{ex}", "ext": ex})

    # 有効なメディアをフィルタリング
    final_media = []
    seen_urls = set()
    for r in results:
        if r["url"] in seen_urls: continue
        content = download_media(r["url"], timeout=10, retries=1)
        if content:
            r["content"] = content
            final_media.append(r)
            seen_urls.add(r["url"])
            if len(final_media) >= 1: break # 最初に見つかった1つを優先

    caption = f"<b>【{site_name}】</b>\n#{entry_id} | {ts}\n\n{text[:400]}"
    kbd = {"inline_keyboard": [[{"text": "View", "url": site_url}, {"text": "Entry", "url": entry_url}]]}

    if not final_media:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML", "reply_markup": json.dumps(kbd)})
        return

    for m in final_media:
        method = "sendVideo" if m["type"] == "video" else "sendPhoto"
        files = {( "video" if m["type"] == "video" else "photo"): (f"file.{m['ext']}", m["content"])}
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
                      data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML", "reply_markup": json.dumps(kbd)},
                      files=files)

# ===== 実行ループ =====
for i, target in enumerate(url_list, start=1):
    target_id = get_identifier(target, i)
    soup = get_soup(target)
    if not soup: continue

    site_name = soup.title.string.split("-")[0].strip() if soup.title else target_id
    items = soup.select("article.resentry")
    
    max_id, history = load_processed_ids(target_id)
    new_max = max_id
    batch_ids = []

    for item in reversed(items):
        try:
            eno_text = item.select_one("span.eno a").get_text(strip=True)
            entry_id = int(re.search(r'\d+', eno_text).group())
        except: continue
        
        if max_id is not None and entry_id <= max_id: continue
        if entry_id in history: continue
        if entry_id in sent_entry_ids: continue
        
        if max_id is None:
            batch_ids.append(entry_id)
            new_max = max(new_max or 0, entry_id)
            continue

        ts = item.select_one("time.date").get_text(strip=True) if item.select_one("time.date") else "N/A"
        txt = item.select_one("div.comment").get_text("\n", strip=True) if item.select_one("div.comment") else ""
        media = [urljoin(target, a["href"]) for a in item.select(".filethumblist li a[href]")]
        media.extend(parse_text_urls(txt))
        
        if media:
            process_and_notify(site_name, target_id, entry_id, ts, txt, target, f"{target}/{entry_id}", list(set(media)))
            sent_entry_ids.add(entry_id)
        
        batch_ids.append(entry_id)
        new_max = max(new_max or 0, entry_id)

    if batch_ids:
        save_processed_ids(target_id, new_max, batch_ids)

sync_repository()
