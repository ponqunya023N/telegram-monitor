import os
import time
import requests
import re
import json
import subprocess
from telethon import TelegramClient, events
from telethon.tl.types import MessageMediaWebPage
import http.client

# --- 設定エリア ---
# GitHub ActionsのSecrets、または環境変数から取得
API_ID = int(os.environ.get("TELEGRAM_API_ID", 0))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL_IDS = os.environ.get("CHANNEL_IDS", "").split(",")  # カンマ区切り
GITHUB_TOKEN = os.environ.get("GH_TOKEN", "")
REPO_NAME = os.environ.get("REPO_NAME", "")  # 例: "user/repo"

# 状態管理用ファイル
PROCESSED_IDS_FILE = "processed_ids.json"

# --- 共通関数 ---

def load_processed_ids():
    if os.path.exists(PROCESSED_IDS_FILE):
        with open(PROCESSED_IDS_FILE, "r") as f:
            return json.load(f)
    return {}

def save_processed_ids(data):
    with open(PROCESSED_IDS_FILE, "w") as f:
        json.dump(data, f, indent=4)

def download_file_with_resume(url, destination, max_retries=5):
    """
    HTTP Rangeヘッダーを使用して、中断されたダウンロードを再開する機能
    """
    for attempt in range(1, max_retries + 1):
        try:
            headers = {}
            mode = 'wb'
            start_pos = 0

            # 既にファイルが存在する場合、サイズを取得して続きから要求
            if os.path.exists(destination):
                start_pos = os.path.getsize(destination)
                headers['Range'] = f'bytes={start_pos}-'
                mode = 'ab'

            with requests.get(url, headers=headers, stream=True, timeout=30) as r:
                # 206 Partial Content: 続きから送信されている
                # 200 OK: サーバーがRange未対応のため最初から送信されている
                if r.status_code == 206:
                    pass 
                elif r.status_code == 200:
                    # 最初から送り直された場合は上書きモードに変更
                    mode = 'wb'
                    start_pos = 0
                elif r.status_code == 416:
                    # Rangeエラー（既に完了している可能性など）
                    return True
                else:
                    r.raise_for_status()

                with open(destination, mode) as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            
            # 正常終了
            return True

        except (requests.exceptions.RequestException, http.client.IncompleteRead) as e:
            current_size = os.path.getsize(destination) if os.path.exists(destination) else 0
            print(f"      [WARN] Download interrupted at {current_size} bytes: {e} (Attempt {attempt}/{max_retries})")
            if attempt < max_retries:
                time.sleep(5) # 再試行前に待機
            else:
                print(f"Error: ERROR] All download attempts failed for: {url}")
                return False

def push_to_github():
    try:
        subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
        subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
        subprocess.run(["git", "add", "."], check=True)
        # 変更がある場合のみコミット
        result = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
        if result.stdout.strip():
            subprocess.run(["git", "commit", "-m", "update files"], check=True)
            subprocess.run(["git", "push"], check=True)
            print(" [LOG] Pushed 1 files.")
        else:
            print(" [LOG] No changes to push.")
    except Exception as e:
        print(f" [ERROR] Git operation failed: {e}")

# --- メインロジック ---

async def main():
    client = TelegramClient('bot_session', API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)

    processed_data = load_processed_ids()
    updated = False

    for channel_id in CHANNEL_IDS:
        channel_id = channel_id.strip()
        print(f"--- Checking: {channel_id} ---")
        
        # チャンネルのエンティティ取得
        entity = await client.get_entity(channel_id)
        last_id = processed_data.get(channel_id, 0)
        
        new_items_found = False
        async for message in client.iter_messages(entity, min_id=last_id, limit=20):
            new_items_found = True
            msg_id = message.id
            print(f"  -> [NEW] Item #{msg_id} detected.")
            
            # メディア解析 (WebPage内のメディアを優先)
            media_urls = []
            if message.media and isinstance(message.media, MessageMediaWebPage):
                webpage = message.media.webpage
                # ユーザー指示: 画像は除外、動画(.mov, .mp4)を優先
                if webpage.display_url:
                    # 動画URLの抽出ロジック（簡易版）
                    # 実際にはHTML解析やAPIが必要な場合があるが、ここではログのURL形式を想定
                    # URLに '_1' を付与しない指示を遵守
                    url = webpage.display_url
                    if any(ext in url.lower() for ext in ['.mov', '.mp4']):
                        media_urls.append(url)

            if media_urls:
                print(f"      [LOG] Analyzing media for #{msg_id}...")
                for url in media_urls:
                    filename = f"{channel_id}_{msg_id}_{os.path.basename(url).split('?')[0]}"
                    if download_file_with_resume(url, filename):
                        print(f"      [LOG] Downloaded: {filename}")
                        updated = True
            else:
                print(f"      [LOG] No relevant media found in #{msg_id}.")

            # 処理済みIDの更新
            if msg_id > processed_data.get(channel_id, 0):
                processed_data[channel_id] = msg_id
                updated = True

        if not new_items_found:
            print(" [LOG] No new items.")

    if updated:
        save_processed_ids(processed_data)
        push_to_github()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
