import os
import requests
from bs4 import BeautifulSoup
import time
import json
import re

# --- 設定項目 ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_URL = os.getenv("TARGET_URL")

# ステータス保存ファイル
STATUS_FILE = "last_status.json"

# 共通ヘッダー（ブラウザを装い403エラーを回避）
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
}

# LOG_WITH_TITLE = False # [2026-02-11] ユーザー指示により無効化

def send_telegram_message(message):
    """Telegramにメッセージを送信する"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        requests.post(url, json=payload, headers=HEADERS)
    except Exception as e:
        print(f"[ERROR] Telegram送信失敗: {e}")

def load_status():
    """前回の状態を読み込む"""
    if os.path.exists(STATUS_FILE):
        with open(STATUS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_status(status):
    """現在の状態を保存する"""
    with open(STATUS_FILE, "w") as f:
        json.dump(status, f, indent=4)

def extract_media_url(page_url, parent_id=None):
    """
    指定されたURLのページから動画URLを抽出する。
    """
    local_headers = HEADERS.copy()
    if parent_id:
        local_headers["Referer"] = page_url
    
    try:
        # 子ページ読み込み前に少し待機
        if parent_id:
            time.sleep(1)
            
        response = requests.get(page_url, headers=local_headers, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 1. ページ内の動画タグを直接探す
        video_tag = soup.find("video")
        if video_tag:
            source_tag = video_tag.find("source")
            if source_tag and source_tag.get("src"):
                return source_tag.get("src")
            if video_tag.get("src"):
                return video_tag.get("src")
        
        # 2. メタタグ(og:video)を探す
        og_video = soup.find("meta", property="og:video") or soup.find("meta", property="og:video:url")
        if og_video and og_video.get("content"):
            return og_video.get("content")

        # 3. 親ページの場合のみ、子ページへのリンク（upup.beなど）を探す
        if parent_id:
            links = soup.find_all("a", href=True)
            for link in links:
                href = link["href"]
                # ユーザー提示の動画URL形式をカバー
                if "upup.be" in href:
                    from urllib.parse import urljoin
                    child_url = urljoin(page_url, href)
                    print(f"     [LOG] Analyzing child page: {child_url}")
                    # 再帰的に動画を探す
                    media_url = extract_media_url(child_url, parent_id=None)
                    if media_url:
                        return media_url

    except Exception as e:
        print(f"[ERROR] ページ解析失敗({page_url}): {e}")
    
    return None

def main():
    print(f"--- Checking: {time.strftime('%Y-%m-%d %H:%M:%S')} ---")
    
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TARGET_URL]):
        print("[ERROR] 設定が不足しています。環境変数を確認してください。")
        return

    # URLをカンマで分割してリスト化
    url_list = [u.strip() for u in TARGET_URL.split(',')]
    prev_status = load_status()
    current_status = {}
    new_found = False

    for url in url_list:
        if not url:
            continue
            
        # URL末尾やID部分をキーにする（5chanの短縮URL末尾などを利用）
        item_id = url.split('/')[-1]
        current_status[item_id] = True
        
        if item_id not in prev_status:
            print(f"  -> [NEW] Item #{item_id} detected.")
            print(f"      [LOG] Analyzing media for {url}...")
            
            # 動画URLを抽出（5chanURL -> upup.be などの遷移を追跡）
            media_url = extract_media_url(url, parent_id=item_id)
            
            if media_url:
                message = f"<b>【新着通知】</b>\n元URL: {url}\n動画: {media_url}"
            else:
                message = f"<b>【新着通知】</b>\n元URL: {url}\n(動画URLは見つかりませんでした)"
            
            send_telegram_message(message)
            new_found = True
        else:
            # 既知のアイテムもcurrent_statusには残す
            pass

    if not new_found:
        print(" [LOG] No new items.")
        
    save_status(current_status)
    print(" [LOG] Execution completed.")

if __name__ == "__main__":
    main()
