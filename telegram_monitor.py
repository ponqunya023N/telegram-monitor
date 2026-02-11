import os
import sys
import re
import time
import json
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

# 1回のアクション実行で送信済みのIDを記録
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
def send_telegram_media_group(board_name, board_id, post_id, posted_at, body_text, target_post_url, media_urls):
    """
    メディアをグループ（アルバム）として送信し、
    その直後に詳細情報と「ブラウザで開く」ボタンを送信する。
    """
    print(f"      [DEBUG] Telegramへ送信を試みます... (Media: {len(media_urls)})")
    
    # 1. メディアの準備 (最大10枚)
    media_group = []
    processed_count = 0
    
    for m_url in media_urls:
        if processed_count >= 10: break
        
        parsed = urlparse(m_url)
        file_id = parsed.path.rstrip("/").split("/")[-1]
        d_char = parsed.netloc.split('.')[0]
        # cdnX.5chan.jp 形式への補正
        base_netloc = parsed.netloc if d_char.startswith("cdn") else f"cdn{d_char}.5chan.jp"

        # 試行URLリスト（画像優先 -> 動画）
        attempt_urls = [
            f"https://{base_netloc}/file/plane/{file_id}.jpg",
            f"https://{base_netloc}/file/{file_id}.mp4",
            f"https://{base_netloc}/file/plane/{file_id}.png",
            f"https://{base_netloc}/file/{file_id}.gif"
        ]
        # 元のURLに拡張子が含まれている場合は最優先
        if "." in file_id: attempt_urls.insert(0, m_url)

        for target_download_url in attempt_urls:
            try:
                # HEADリクエストで存在確認
                r = requests.head(target_download_url, headers=headers, timeout=10)
                if r.status_code == 200:
                    # Content-Type または拡張子から種別判断
                    content_type = r.headers.get('Content-Type', '').lower()
                    ext = target_download_url.split('.')[-1].lower()
                    
                    if "video" in content_type or ext in ["mp4", "mov", "webm"]:
                        media_type = "video"
                    else:
                        media_type = "photo"
                        
                    media_group.append({"type": media_type, "media": target_download_url})
                    processed_count += 1
                    break
            except: continue

    # 2. メディアグループの送信
    if media_group:
        send_group_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMediaGroup"
        # 最初のメディアにのみキャプションをつけることも可能ですが、別途メッセージを送るためここでは送信のみ
        requests.post(send_group_url, data={"chat_id": TELEGRAM_CHAT_ID, "media": json.dumps(media_group)})

    # 3. テキストとインラインボタンの送信
    # 冒頭300文字程度を引用
    summary_text = body_text[:300] + ("..." if len(body_text) > 300 else "")
    
    message_text = (
        f"<b>【{board_name}】</b>\n"
        f"投稿番号: #{post_id}\n"
        f"投稿日時: {posted_at}\n\n"
        f"{summary_text}"
    )
    
    keyboard = {
        "inline_keyboard": [[
            {"text": "🌐 ブラウザで詳細を確認", "url": target_post_url}
        ]]
    }
    
    send_msg_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message_text,
        "parse_mode": "HTML", # 太字を有効にする
        "reply_markup": json.dumps(keyboard)
    }
    
    try:
        resp = requests.post(send_msg_url, data=payload)
        if resp.status_code == 200:
            print(f"      [SUCCESS] 投稿#{post_id} の送信完了。")
        else:
            print(f"      [ERROR] Telegram送信失敗: {resp.text}")
    except Exception as e:
        print(f"      [ERROR] 通信エラー: {e}")

# ===== メイン処理 =====
for target in url_list:
    board_id = get_board_id(target)
    print(f"--- Checking board: {board_id} ---")
    try:
        resp = requests.get(target, headers=headers, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f" [ERROR] ボード読み込み失敗 ({target}): {e}")
        continue

    soup = BeautifulSoup(resp.text, "html.parser")
    
    # 掲示板タイトル取得
    board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
    
    articles = soup.select("article.resentry")
    if not articles: continue

    # 最新の件数を対象とする（既読IDより大きいものをすべて取得）
    last_post_id = load_last_post_id(board_id)
    newest_post_id = last_post_id if last_post_id else 0
    
    # 逆順（古い順）に処理して新着を漏らさない
    for article in articles:
        eno_tag = article.select_one("span.eno a")
        if eno_tag is None: continue 
        try:
            post_id = int("".join(filter(str.isdigit, eno_tag.get_text(strip=True))))
        except: continue

        if last_post_id is not None and post_id <= last_post_id:
            continue
            
        if post_id in sent_post_ids: continue
        
        # newest_post_id の更新
        if post_id > newest_post_id: newest_post_id = post_id
        
        print(f"  -> [NEW] 投稿#{post_id} を処理中...")
        time_tag = article.select_one("time.date")
        posted_at = time_tag.get_text(strip=True) if time_tag else "N/A"
        comment_div = article.select_one("div.comment")
        body_text = comment_div.get_text("\n", strip=True) if comment_div else ""

        media_urls = []
        
        # 本文からの抽出
        urls_in_body = extract_urls(body_text)
        for u in urls_in_body:
            if "disp" in u or "upup.be" in u: media_urls.append(u)

        # サムネイルリストからの抽出
        thumblist = article.select(".filethumblist li")
        for li in thumblist:
            a_tag = li.select_one("a[href]")
            if a_tag:
                abs_url = urljoin(target, a_tag.get("href"))
                media_urls.append(abs_url)

        if not media_urls:
            print(f"  -> 投稿#{post_id} は画像/動画がないためスキップ。")
            continue

        # 投稿への直接URL（掲示板URLに番号を付加）
        # クエリパラメータ ?from=new2 などがついている場合を考慮
        base_target = target.split('?')[0].rstrip('/')
        target_post_url = f"{base_target}/{post_id}"
        
        send_telegram_media_group(
            board_name, board_id, post_id, posted_at, body_text, 
            target_post_url, list(dict.fromkeys(media_urls))
        )
        sent_post_ids.add(post_id)

    if newest_post_id > (last_post_id if last_post_id else 0):
        save_last_post_id(board_id, newest_post_id)
