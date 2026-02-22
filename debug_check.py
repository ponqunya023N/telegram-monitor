import requests
from bs4 import BeautifulSoup

# 調査対象のURL（あなたが提示した2つのURLをテストします）
test_urls = [
    "https://upup.be/vuvciQXc82?a=KKXCQ7Xa",
    "https://upup.be/JGIIbzUmpi?a=KKXCQ7Xa"
]

headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

def debug_page(url):
    print(f"\n=== Testing URL: {url} ===")
    try:
        res = requests.get(url, headers=headers, timeout=10)
        print(f"Status Code: {res.status_code}")
        
        if res.status_code == 200:
            soup = BeautifulSoup(res.text, "html.parser")
            
            # 1. videoタグがあるか確認
            video = soup.find("video")
            print(f"Video tag found: {True if video else False}")
            if video:
                print(f"Video src: {video.get('src')}")
                # sourceタグも確認
                source = video.find("source")
                if source:
                    print(f"Source tag src: {source.get('src')}")

            # 2. ページ内の全てのリンク（<a>タグ）を書き出す（上位10件程度）
            print("--- Found links (first 10) ---")
            links = soup.find_all("a", href=True)
            for i, a in enumerate(links[:10]):
                print(f"Link {i}: {a['href']}")

            # 3. HTMLの全体像を少しだけ表示（構造把握のため）
            print("--- HTML Snippet (Body) ---")
            if soup.body:
                # 構造を把握するため、bodyの最初の300文字を表示
                print(soup.body.get_text()[:300].replace('\n', ' '))

    except Exception as e:
        print(f"Error: {e}")

# 実行
for target in test_urls:
    debug_page(target)
