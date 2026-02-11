import os
import urllib.parse
import urllib.request

def send_telegram_test():
    # GitHub Secretsから情報を取得
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    
    if not token or not chat_id:
        print("[ERROR] TELEGRAM_BOT_TOKEN または TELEGRAM_CHAT_ID が設定されていません。")
        return

    # 送信するメッセージ
    text = "Hello! これは新しいリポジトリからのテスト送信です。成功しています！"
    
    # Telegram APIのURL作成
    # https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>&text=<TEXT>
    encoded_text = urllib.parse.quote(text)
    url = f"https://api.telegram.org/bot{token}/sendMessage?chat_id={chat_id}&text={encoded_text}"

    print(f"[INFO] Telegramへ送信を試みます...")
    
    try:
        with urllib.request.urlopen(url) as response:
            res_body = response.read().decode("utf-8")
            if response.getcode() == 200:
                print("[OK] 送信成功！Telegramを確認してください。")
            else:
                print(f"[ERROR] 送信失敗。ステータスコード: {response.getcode()}")
                print(f"レスポンス: {res_body}")
    except Exception as e:
        print(f"[ERROR] 例外が発生しました: {e}")

if __name__ == "__main__":
    send_telegram_test()
