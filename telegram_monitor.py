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
    """URL全体から一意のファイル名用IDを生成する"""
    # https:// を除去し、記号をアンダースコアに置換
    safe_id = re.sub(r'https?://', '', url)
    safe_id = re.sub(r'[\/:?=&]', '_', safe_id)
    return safe_id

# --- 変更前元のコード（極力残すルールに則りコメントアウト） ---
# def load_last_post_id(board_id: str):
#     fname = f"last_post_id_{board_id}.txt"
#     if not os.path.exists(fname): return None
#     try:
#         with open(fname, "r", encoding="utf-8") as f:
#             content = f.read().strip()
#             return int(content) if content else None
#     except: return None
# -------------------------------------------------------------

# --- 変更後：あなたの案を採用し、IDのリストを読み込むように修正 ---
def load_last_post_ids(board_id: str):
    """txtファイルから前回の投稿番号のリストを読み込みます"""
    fname = f"last_post_id_{board_id}.txt"
    if not os.path.exists(fname): return []
    try:
        with open(fname, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if not content: return []
            # カンマ区切りでリスト化。古い形式(単一の数字)のtxtファイルにもそのまま対応できます
            return [int(x) for x in content.split(",") if x.strip().isdigit()]
    except: return []
# -------------------------------------------------------------

# --- 変更前元のコード ---
# def save_last_post_id_local(board_id: str, post_id: int):
#     """ローカルファイルのみ更新し、更新ファイルリストに追加"""
#     fname = f"last_post_id_{board_id}.txt"
#     with open(fname, "w", encoding="utf-8") as f:
#         f.write(str(post_id))
#     if fname not in updated_files:
#         updated_files.append(fname)
# -----------------------

# --- 変更後：通知したIDのリストをカンマ区切りで保存するように修正 ---
def save_last_post_ids_local(board_id: str, post_ids: list):
    """今回通知した複数の投稿番号をカンマ区切りのリスト形式で保存します"""
    fname = f"last_post_id_{board_id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        # リストの中身をカンマ(,)で繋いでtxtに書き込みます
        f.write(",".join(map(str, sorted(post_ids))))
    if fname not in updated_files:
        updated_files.append(fname)
# -------------------------------------------------------------

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
            
            status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
            if status.stdout.strip():
                subprocess.run(["git", "commit", "-m", "update multiple last_ids"], check=True)
                subprocess.run(["git", "pull", "--rebase"], check=False)
                subprocess.run(["git", "push"], check=True)
                print(f" [LOG] {len(updated_files)}件のIDファイルをプッシュしました。")
        except Exception as e:
            print(f" [ERROR] Push failed: {e}")

# ===== 送信ロジック・ユーティリティ =====
def extract_urls(text: str):
    found = URL_PATTERN.findall(text)
    unique_urls = sorted(list(set(found)))
    filtered_urls = []
    
    for url in unique_urls:
        if "/read.cgi/" in url:
            continue
        filtered_urls.append(url)
    return filtered_urls

def resolve_external_media(url):
    """upup.beやimgef.comなどの外部ページから動画URLを抽出する"""
    if "upup.be" in url or "imgef.com" in url:
        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                soup = BeautifulSoup(res.text, "html.parser")
                video_tag = soup.find("video")
                if video_tag:
                    src = video_tag.get("src") or (video_tag.find("source").get("src") if video_tag.find("source") else None)
                    if src:
                        full_url = urljoin(url, src)
                        ext = full_url.split(".")[-1].split("?")[0]
                        return {"type": "video", "url": full_url, "ext": ext}
                for a in soup.find_all("a", href=True):
                    if ".mov" in a["href"].lower() or ".mp4" in a["href"].lower():
                        full_url = urljoin(url, a["href"])
                        ext = full_url.split(".")[-1].split("?")[0]
                        return {"type": "video", "url": full_url, "ext": ext}
        except: pass
    return None

def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
    print(f"      [LOG] 投稿#{post_id} のメディア解析開始")
    valid_media_list = []
    
    for m_url in media_urls:
        external = resolve_external_media(m_url)
        if external:
            valid_media_list.append(external)
            continue

        parsed = urlparse(m_url)
        raw_file_id = parsed.path.rstrip("/").split("/")[-1]
        file_id = os.path.splitext(raw_file_id)[0] 
        
        netloc = parsed.netloc
        if not netloc.startswith("cdn") and ".5chan.jp" in netloc:
            subdomain = netloc.split('.')[0]
            netloc = f"cdn{subdomain}.5chan.jp"

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
    keyboard = {"inline_keyboard": [[{"text": "掲示板", "url": board_url}, {"text": "投稿", "url": target_post_url}]]}

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
    
    # --- 変更前 ---
    # last_id = load_last_post_id(board_id)
    # new_last_id = last_id
    # -------------
    
    # --- 変更後：リストを取得し、その中の最大値も把握しておく ---
    last_ids_list = load_last_post_ids(board_id)
    max_last_id = max(last_ids_list) if last_ids_list else None
    
    # 今回の処理で新たに通知対象となった投稿番号だけを入れる空のリストを準備します
    current_batch_ids = []
    # -------------------------------------------------------

    for article in reversed(articles):
        try:
            eno_text = article.select_one("span.eno a").get_text(strip=True)
            post_id = int(re.search(r'\d+', eno_text).group())
        except: continue

        # --- 変更前 ---
        # if last_id is not None and post_id <= last_id: continue
        # -------------
        
        # --- 変更後：あなたの案に基づく二重の重複防止チェック ---
        # 過去の最大ID以下の古い投稿、または「前回のリスト」にすでに含まれている場合はスキップします
        if max_last_id is not None and post_id <= max_last_id: continue
        if post_id in last_ids_list: continue
        # ------------------------------------------------------

        if post_id in sent_post_ids: continue
        
        # --- 変更前 ---
        # if last_id is None:
        #     new_last_id = max(new_last_id or 0, post_id)
        #     continue
        # -------------
        
        # --- 変更後：初回実行時の処理 ---
        if max_last_id is None:
            # 初回は通知せずに投稿番号だけをリストに控えておきます
            current_batch_ids.append(post_id)
            continue
        # --------------------------------

        print(f"  -> [NEW] 投稿#{post_id} を検知しました。")
        posted_at = article.select_one("time.date").get_text(strip=True) if article.select_one("time.date") else "N/A"
        body_text = article.select_one("div.comment").get_text("\n", strip=True) if article.select_one("div.comment") else ""
        
        media_urls = [urljoin(target, a["href"]) for a in article.select(".filethumblist li a[href]")]
        extracted = extract_urls(body_text)
        
        if extracted or media_urls:
            media_urls.extend(extracted)
            send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, target, f"{target}/{post_id}", list(set(media_urls)))
            sent_post_ids.add(post_id)
        
        # --- 変更前 ---
        # new_last_id = max(new_last_id or 0, post_id)
        # -------------
        
        # --- 変更後：通知した投稿番号をリストに追加して控えておく ---
        current_batch_ids.append(post_id)
        # ------------------------------------------------------

    # --- 変更前 ---
    # if new_last_id and new_last_id != last_id:
    #     save_last_post_id_local(board_id, new_last_id)
    # else:
    #     print(" [LOG] 新着なし")
    # -------------
    
    # --- 変更後：あなたの「リストに変化があった時のみ保存する」というルールの実装 ---
    if current_batch_ids:
        # 新しく通知したもの（または初回読み込み分）があれば、そのリストを保存します
        save_last_post_ids_local(board_id, current_batch_ids)
    else:
        # 新着が全くない場合はリストを上書きせず、何もしません（あなたの案の通りです）
        print(" [LOG] 新着なし")
    # --------------------------------------------------------------------------

commit_and_push_all()
