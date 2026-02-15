import os
import sys
import re
import time
import json
import io
import subprocess
import hashlib # 追加：URLをハッシュ化するための標準ライブラリ
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
# --- 変更前元のコード ---
# def get_board_id(url: str) -> str:
#     """URL全体から一意のファイル名用IDを生成する"""
#     safe_id = re.sub(r'https?://', '', url)
#     safe_id = re.sub(r'[\/:?=&]', '_', safe_id)
#     return safe_id
# -----------------------

# --- 変更後：URLを完全に秘匿化したハッシュ文字列に変換し、連番を付ける ---
def get_board_id(url: str, index: int) -> str:
    """URLを解読不可のハッシュにし、Secretsの順番(index)を先頭に付与します"""
    hashed = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
    return f"{index:02d}_{hashed}"
# -----------------------------------------------------------------


# --- 変更前元のコード ---
# def load_last_post_ids(board_id: str):
#     """txtファイルから前回の投稿番号のリストを読み込みます"""
#     fname = f"last_post_id_{board_id}.txt"
#     if not os.path.exists(fname): return []
#     try:
#         with open(fname, "r", encoding="utf-8") as f:
#             content = f.read().strip()
#             if not content: return []
#             return [int(x) for x in content.split(",") if x.strip().isdigit()]
#     except: return []
# -----------------------

# --- 変更後：A（最大番号）とB（通知リスト）を2行で分けて管理するように修正 ---
def load_last_post_ids_ab(board_id: str):
    """txtファイルから1行目(A:最大番号)と2行目(B:リスト)を読み込みます"""
    fname = f"last_post_id_{board_id}.txt"
    if not os.path.exists(fname): return None, []
    try:
        with open(fname, "r", encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
            if not lines: return None, []
            
            # 1行目：A (最大番号)
            max_id = int(lines[0]) if lines[0].strip().isdigit() else None
            
            # 2行目：B (前回通知した番号のリスト)
            id_list = []
            if len(lines) > 1:
                id_list = [int(x) for x in lines[1].split(",") if x.strip().isdigit()]
            return max_id, id_list
    except: return None, []
# -------------------------------------------------------------


# --- 変更前元のコード ---
# def save_last_post_ids_local(board_id: str, post_ids: list):
#     """今回通知した複数の投稿番号をカンマ区切りのリスト形式で保存します"""
#     fname = f"last_post_id_{board_id}.txt"
#     with open(fname, "w", encoding="utf-8") as f:
#         f.write(",".join(map(str, sorted(post_ids))))
#     if fname not in updated_files:
#         updated_files.append(fname)
# -----------------------

# --- 変更後：AとBを分けて保存するように修正（テストでの手動変更をしやすくするため） ---
def save_last_post_ids_local_ab(board_id: str, max_id: int, post_ids: list):
    """1行目に最大番号(A)、2行目に通知リスト(B)を保存します"""
    fname = f"last_post_id_{board_id}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write(f"{max_id}\n")
        f.write(",".join(map(str, sorted(post_ids))))
    if fname not in updated_files:
        updated_files.append(fname)
# --------------------------------------------------------------------------------


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
# --- 変更前 ---
# for target in url_list:
#     board_id = get_board_id(target)
# --- 変更後：順番(index)を1から数えて一緒に取得する ---
for index, target in enumerate(url_list, start=1):
    board_id = get_board_id(target, index)
# ------------------------------------------------

    try:
        resp = requests.get(target, headers=headers, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
    # --- 変更前 ---
    # except: continue
    # --- 変更後：エラー時にURLを出さずログを隠蔽 ---
    except Exception as e: 
        print(f" [ERROR] 掲示板 {board_id} の取得に失敗しました。（URLは秘匿されています）")
        continue
    # ---------------------------------------------

    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    print(f"--- Checking board: {board_id} ---")
    
    articles = soup.select("article.resentry")
    
    # --- 変更前 ---
    # last_ids_list = load_last_post_ids(board_id)
    # max_last_id = max(last_ids_list) if last_ids_list else None
    # current_batch_ids = []
    # -------------
    
    # --- 変更後：A（最大番号）とB（リスト）を別々に読み込み ---
    saved_max_id, last_ids_list = load_last_post_ids_ab(board_id)
    # 処理中の新しい最大値を記録する変数
    new_max_id = saved_max_id
    current_batch_ids = []
    # -------------------------------------------------------

    for article in reversed(articles):
        try:
            eno_text = article.select_one("span.eno a").get_text(strip=True)
            post_id = int(re.search(r'\d+', eno_text).group())
        except: continue
        
        # --- 変更前 ---
        # if max_last_id is not None and post_id <= max_last_id: continue
        # if post_id in last_ids_list: continue
        # -------------
        
        # --- 変更後：A（saved_max_id）とB（last_ids_list）を使ったチェック ---
        # テストで1行目のAを手動で下げた場合、ここをすり抜けて通知テストが行われます
        if saved_max_id is not None and post_id <= saved_max_id: continue
        if post_id in last_ids_list: continue
        # ------------------------------------------------------

        if post_id in sent_post_ids: continue
        
        # --- 変更前 ---
        # if max_last_id is None:
        #     current_batch_ids.append(post_id)
        #     continue
        # -------------
        
        # --- 変更後：初回実行時の処理（最大値を記録する処理を追加） ---
        if saved_max_id is None:
            current_batch_ids.append(post_id)
            new_max_id = max(new_max_id or 0, post_id)
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
        # current_batch_ids.append(post_id)
        # -------------
        
        # --- 変更後：通知した投稿番号をリストに追加し、最大値も更新しておく ---
        current_batch_ids.append(post_id)
        new_max_id = max(new_max_id or 0, post_id)
        # ------------------------------------------------------

    
    # --- 変更前 ---
    # if current_batch_ids:
    #     save_last_post_ids_local(board_id, current_batch_ids)
    # else:
    #     print(" [LOG] 新着なし")
    # -------------
    
    # --- 変更後：A（最大値）とB（今回処理したリスト）を保存 ---
    if current_batch_ids:
        save_last_post_ids_local_ab(board_id, new_max_id, current_batch_ids)
    else:
        print(" [LOG] 新着なし")
    # --------------------------------------------------------------------------

commit_and_push_all()
