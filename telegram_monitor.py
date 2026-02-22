import os
import requests
from bs4 import BeautifulSoup
import time
import json
import re

# --- 設定項目 ---
# 環境変数から取得（GitHub Secrets等での設定を推奨）
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TARGET_URL = os.getenv("TARGET_URL")

# ステータス保存ファイル
STATUS_FILE = "last_status.json"

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
        requests.post(url, json=payload)
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
    子ページがある場合はその中身も解析する。
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Referer": TARGET_URL # 親ページからの遷移であることを示す
    }
    
    try:
        response = requests.get(page_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 1. ページ内の動画タグを直接探す
        # <video>タグや<source>タグを検索
        video_tag = soup.find("video")
        if video_tag:
            source_tag = video_tag.find("source")
            if source_tag and source_tag.get("src"):
                return source_tag.get("src")
            if video_tag.get("src"):
                return video_tag.get("src")
        
        # 2. メタタグ(og:video)を探す（SNS共有用などの隠れたURL）
        og_video = soup.find("meta", property="og:video") or soup.find("meta", property="og:video:url")
        if og_video and og_video.get("content"):
            return og_video.get("content")

        # 3. 親ページの場合、子ページへのリンクを探して再帰的に探索
        if parent_id:
            links = soup.find_all("a", href=True)
            for link in links:
                href = link["href"]
                # IDが含まれている、または特定のパターンを持つリンクをチェック
                if parent_id in href or "/vuvci" in href:
                    # 相対パスを絶対パスに変換
                    child_url = href
                    if href.startswith("/"):
                        from urllib.parse import urljoin
                        child_url = urljoin(page_url, href)
                    
                    # 子ページの中身を見に行く
                    print(f"     [LOG] Analyzing child page: {child_url}")
                    media_url = extract_media_url(child_url) # 子ページにはparent_idを渡さない（無限ループ防止）
                    if media_url:
                        return media_url

    except Exception as e:
        print(f"[ERROR] ページ解析失敗({page_url}): {e}")
    
    return None

def main():
    print(f"--- Checking: {time.strftime('%H_%M%S%f')[:-9]} ---")
    
    if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TARGET_URL]):
        print("[ERROR] 設定が不足しています。環境変数を確認してください。")
        return

    prev_status = load_status()
    
    try:
        response = requests.get(TARGET_URL, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        
        # 投稿アイテムの特定 (サイトの構造に合わせて調整)
        # ここでは例として 'item' クラスを持つ要素をループ
        items = soup.find_all(class_="item") 
        
        # もし特定のクラスがない場合、すべてのリンクから投稿IDを抽出する等の処理が必要
        # 現状は前回のロジックを維持しつつ、子ページ解析を強化
        
        new_found = False
        current_status = {}

        # 投稿を解析（逆順に処理して新しいものを後に保存）
        for item in items:
            # 投稿IDの抽出 (例: KKXCQ7Xa)
            item_id_match = re.search(r"id=(\w+)", str(item)) # ID抽出ロジック（サイトに合わせる）
            if not item_id_match:
                continue
            
            item_id = item_id_match.group(1)
            current_status[item_id] = True
            
            if item_id not in prev_status:
                print(f"  -> [NEW] Item #{item_id} detected.")
                print(f"      [LOG] Analyzing media for #{item_id}...")
                
                # 動画URLの抽出（子ページまで追跡）
                media_url = extract_media_url(TARGET_URL, parent_id=item_id)
                
                if media_url:
                    message = f"<b>【新着通知】</b>\nID: {item_id}\nURL: {TARGET_URL}\n動画: {media_url}"
                    send_telegram_message(message)
                else:
                    # 動画が見つからなくてもページURLは通知する
                    message = f"<b>【新着通知】</b>\nID: {item_id}\nURL: {TARGET_URL}\n(動画URLは取得できませんでした)"
                    send_telegram_message(message)
                
                new_found = True

        if not new_found:
            print(" [LOG] No new items.")
            
        save_status(current_status)

    except Exception as e:
        print(f"[ERROR] メイン処理失敗: {e}")

    print(" [LOG] Pushed 1 files.") # 実行完了ログ

if __name__ == "__main__":
    main()
