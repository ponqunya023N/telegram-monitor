# L001 | import os
# L002 | import sys
# L003 | import re
# L004 | import time
# L005 | import json
# L006 | import io
# L007 | import subprocess
# L008 | import hashlib # IDをハッシュ化するための標準ライブラリ
# L009 | from urllib.parse import urljoin, urlparse
# L010 | 
# L011 | import requests
# L012 | from bs4 import BeautifulSoup
# L013 | 
# L014 | # ===== 設定スイッチ =====
# L015 | LOG_WITH_TITLE = False 
# L016 | 
# L017 | # ===== 定数・環境変数 =====
# L018 | TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
# L019 | TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
# L020 | TARGET_URL = os.environ.get("TARGET_URL")
# L021 | 
# L022 | # --- 秘匿設定 ---
# L023 | DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "") 
# L024 | EXTERNAL_DOMAINS = os.environ.get("EXTERNAL_DOMAINS", "").split(",") 
# L025 | MEDIA_PREFIX = "cdn" 
# L026 | # ----------------
# L027 | 
# L028 | if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TARGET_URL]):
# L029 |     print("Missing environment variables.")
# L030 |     sys.exit(1)
# L031 | 
# L032 | url_list = [u.strip() for u in TARGET_URL.split(",") if u.strip()]
# L033 | headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
# L034 | sent_post_ids = set()
# L035 | URL_PATTERN = re.compile(r"https?://[\w/:%#\$&\?\(\)~\.=\+\-]+", re.IGNORECASE)
# L036 | 
# L037 | # 更新があったファイルを記録するリスト
# L038 | updated_files = []
# L039 | 
# L040 | # ===== 状態管理 =====
# L041 | def get_board_id(url: str, index: int) -> str:
# L042 |     """URLを識別用の符号に変換します"""
# L043 |     hashed = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
# L044 |     return f"{index:02d}_{hashed}"
# L045 | 
# L046 | def load_last_post_ids_ab(board_id: str):
# L047 |     """保存されたID情報を読み込みます"""
# L048 |     fname = f"last_post_id_{board_id}.txt"
# L049 |     if not os.path.exists(fname): return None, []
# L050 |     try:
# L051 |         with open(fname, "r", encoding="utf-8") as f:
# L052 |             lines = f.read().strip().splitlines()
# L053 |             if not lines: return None, []
# L054 |             
# L055 |             # 1行目：最大番号
# L056 |             max_id = int(lines[0]) if lines[0].strip().isdigit() else None
# L057 |             
# L058 |             # 2行目：通知済みリスト
# L059 |             id_list = []
# L060 |             if len(lines) > 1:
# L061 |                 id_list = [int(x) for x in lines[1].split(",") if x.strip().isdigit()]
# L062 |             return max_id, id_list
# L063 |     except: return None, []
# L064 | 
# L065 | def save_last_post_ids_local_ab(board_id: str, max_id: int, post_ids: list):
# L066 |     """最新のID情報を保存します"""
# L067 |     fname = f"last_post_id_{board_id}.txt"
# L068 |     with open(fname, "w", encoding="utf-8") as f:
# L069 |         f.write(f"{max_id}\n")
# L070 |         f.write(",".join(map(str, sorted(post_ids))))
# L071 |     if fname not in updated_files:
# L072 |         updated_files.append(fname)
# L073 | 
# L074 | def commit_and_push_all():
# L075 |     """IDファイルの更新を確定させます"""
# L076 |     if not updated_files:
# L077 |         print(" [LOG] No ID files to update.")
# L078 |         return
# L079 |     
# L080 |     if os.environ.get("GITHUB_ACTIONS") == "true":
# L081 |         try:
# L082 |             subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
# L083 |             subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
# L084 |             for f in updated_files:
# L085 |                 subprocess.run(["git", "add", f], check=True)
# L086 |             
# L087 |             status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
# L088 |             if status.stdout.strip():
# L089 |                 subprocess.run(["git", "commit", "-m", "update files"], check=True)
# L090 |                 subprocess.run(["git", "pull", "--rebase"], check=False)
# L091 |                 subprocess.run(["git", "push"], check=True)
# L092 |                 print(f" [LOG] Pushed {len(updated_files)} files.")
# L093 |         except Exception as e:
# L094 |             print(f" [ERROR] Push failed: {e}")
# L095 | 
# L096 | # ===== 通信ユーティリティ =====
# L097 | 
# L098 | def fetch_content_with_retry(url, timeout=30, retries=5):
# L099 |     """ストリーミングとRangeヘッダーを利用して確実にダウンロードします"""
# L100 |     content = bytearray()
# L101 |     
# L102 |     for i in range(retries):
# L103 |         try:
# L104 |             current_headers = headers.copy()
# L105 |             if len(content) > 0:
# L106 |                 current_headers['Range'] = f"bytes={len(content)}-"
# L107 |                 
# L108 |             with requests.get(url, headers=current_headers, timeout=timeout, stream=True) as res:
# L109 |                 if res.status_code in [200, 206]:
# L110 |                     if res.status_code == 200:
# L111 |                         content = bytearray()
# L112 |                         
# L113 |                     for chunk in res.iter_content(chunk_size=16384):
# L114 |                         if chunk:
# L115 |                             content.extend(chunk)
# L116 |                     
# L117 |                     expected_size = res.headers.get('Content-Length')
# L118 |                     if expected_size and res.status_code == 200 and int(expected_size) != len(content):
# L119 |                         raise requests.exceptions.ContentDecodingError("Incomplete download")
# L120 |                     
# L121 |                     return bytes(content)
# L122 |                 
# L123 |                 if res.status_code == 404:
# L124 |                     return None
# L125 |                     
# L126 |                 print(f"      [WARN] HTTP {res.status_code} for {url} (Attempt {i+1}/{retries})")
# L127 |                 
# L128 |         except (requests.exceptions.RequestException, Exception) as e:
# L129 |             wait_time = 2 ** (i + 1)
# L130 |             print(f"      [WARN] Download error on {url}: {e} (Attempt {i+1}/{retries}). Retrying in {wait_time}s...")
# L131 |             time.sleep(wait_time)
# L132 |             
# L133 |     return None
# L134 | 
# L135 | def fetch_page_soup(url, timeout=15):
# L136 |     """HTMLを取得してBeautifulSoupオブジェクトを返します"""
# L137 |     try:
# L138 |         res = requests.get(url, headers=headers, timeout=timeout)
# L139 |         if res.status_code == 200:
# L140 |             return BeautifulSoup(res.text, "html.parser")
# L141 |     except: pass
# L142 |     return None
# L143 | 
# L144 | # ===== 解析・送信ロジック =====
# L145 | def extract_urls(text: str):
# L146 |     """テキストからURLを抽出します"""
# L147 |     found = URL_PATTERN.findall(text)
# L148 |     unique_urls = sorted(list(set(found)))
# L149 |     filtered_urls = []
# L150 |     
# L151 |     for url in unique_urls:
# L152 |         if "/read.cgi/" in url:
# L153 |             continue
# L154 |         filtered_urls.append(url)
# L155 |     return filtered_urls
# L156 | 
# L157 | def resolve_external_media(url, depth=0):
# L158 |     """外部ページからメディアを抽出します（OGP対応）"""
# L159 |     if depth > 1:
# L160 |         return None
# L161 | 
# L162 |     parsed_url = urlparse(url)
# L163 |     is_target = any(domain in url for domain in EXTERNAL_DOMAINS if domain) or "upup.be" in parsed_url.netloc or "5chan.jp" in parsed_url.netloc
# L164 |     
# L165 |     if is_target:
# L166 |         soup = fetch_page_soup(url)
# L167 |         if soup:
# L168 |             try:
# L169 |                 found_media_in_page = []
# L170 |                 
# L171 |                 # OGP解析
# L172 |                 og_video = soup.find("meta", property="og:video") or soup.find("meta", attrs={"name": "twitter:player:stream"})
# L173 |                 if og_video and og_video.get("content"):
# L174 |                     v_url = urljoin(url, og_video["content"])
# L175 |                     ext = v_url.split(".")[-1].split("?")[0].lower()
# L176 |                     found_media_in_page.append({"type": "video", "url": v_url, "ext": ext})
# L177 | 
# L178 |                 og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
# L179 |                 if og_image and og_image.get("content"):
# L180 |                     i_url = urljoin(url, og_image["content"])
# L181 |                     ext = i_url.split(".")[-1].split("?")[0].lower()
# L182 |                     found_media_in_page.append({"type": "photo", "url": i_url, "ext": ext})
# L183 | 
# L184 |                 # タグ解析
# L185 |                 video_tag = soup.find("video")
# L186 |                 if video_tag:
# L187 |                     src = video_tag.get("src") or (video_tag.find("source").get("src") if video_tag.find("source") else None)
# L188 |                     if src:
# L189 |                         full_v_url = urljoin(url, src)
# L190 |                         ext = full_v_url.split(".")[-1].split("?")[0].lower()
# L191 |                         m_obj = {"type": "video", "url": full_v_url, "ext": ext}
# L192 |                         if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)
# L193 | 
# L194 |                 for a in soup.find_all("a", href=True):
# L195 |                     href_lower = a["href"].lower()
# L196 |                     if any(ext in href_lower for ext in [".mp4", ".mov", ".wmv", ".webm"]):
# L197 |                         full_v_url = urljoin(url, a["href"])
# L198 |                         ext = full_v_url.split(".")[-1].split("?")[0].lower()
# L199 |                         m_obj = {"type": "video", "url": full_v_url, "ext": ext}
# L200 |                         if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)
# L201 | 
# L202 |                 for img in soup.find_all("img", src=True):
# L203 |                     src = img.get("src")
# L204 |                     if src and not any(x in src for x in ["qrcode", "logo", "icon"]):
# L205 |                         full_img_url = urljoin(url, src)
# L206 |                         ext = full_img_url.split(".")[-1].split("?")[0].lower()
# L207 |                         if ext in ["jpg", "jpeg", "png", "gif", "webp"]:
# L208 |                             m_obj = {"type": "photo", "url": full_img_url, "ext": ext}
# L209 |                             if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)
# L210 | 
# L211 |                 if depth == 1 and found_media_in_page:
# L212 |                     return found_media_in_page
# L213 | 
# L214 |                 if depth == 0:
# L215 |                     base_id = parsed_url.path.strip('/').split('/')[-1] 
# L216 |                     child_links = []
# L217 |                     for a in soup.find_all("a", href=True):
# L218 |                         full_child_url = urljoin(url, a["href"])
# L219 |                         if parsed_url.netloc in full_child_url and base_id in full_child_url and full_child_url != url:
# L220 |                             child_links.append(full_child_url)
# L221 |                     
# L222 |                     found_media_list = found_media_in_page
# L223 |                     for child_url in set(child_links):
# L224 |                         child_results = resolve_external_media(child_url, depth=1)
# L225 |                         if child_results:
# L226 |                             for r in child_results:
# L227 |                                 if r not in found_media_list: found_media_list.append(r)
# L228 |                     
# L229 |                     return found_media_list
# L230 |             except Exception as e:
# L231 |                 print(f"      [ERROR] BS4 analysis failed for {url}: {e}")
# L232 |     return None
# L233 | 
# L234 | def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
# L235 |     """解析結果をTelegramへ送信します"""
# L236 |     print(f"      [LOG] Analyzing media for #{post_id}...")
# L237 |     valid_media_list = []
# L238 |     
# L239 |     for m_url in media_urls:
# L240 |         external = resolve_external_media(m_url)
# L241 |         if external:
# L242 |             if isinstance(external, list): valid_media_list.extend(external)
# L243 |             else: valid_media_list.append(external)
# L244 |             continue
# L245 | 
# L246 |         parsed = urlparse(m_url)
# L247 |         raw_file_id = parsed.path.rstrip("/").split("/")[-1]
# L248 |         file_id = os.path.splitext(raw_file_id)[0] 
# L249 |         
# L250 |         netloc = parsed.netloc
# L251 |         if DOMAIN_SUFFIX and DOMAIN_SUFFIX in netloc:
# L252 |             if not netloc.startswith(MEDIA_PREFIX):
# L253 |                 subdomain = netloc.split('.')[0]
# L254 |                 netloc = f"{MEDIA_PREFIX}{subdomain}{DOMAIN_SUFFIX}"
# L255 | 
# L256 |         candidates = []
# L257 |         for ext in ["mp4", "mov", "webm", "gif"]:
# L258 |             candidates.append({"type": "video", "url": f"https://{netloc}/file/{file_id}.{ext}", "ext": ext})
# L259 |         for ext in ["png", "jpg", "jpeg"]:
# L260 |             candidates.append({"type": "photo", "url": f"https://{netloc}/file/plane/{file_id}.{ext}", "ext": ext})
# L261 | 
# L262 |         for attempt in candidates:
# L263 |             content = fetch_content_with_retry(attempt["url"], timeout=10, retries=1)
# L264 |             if content:
# L265 |                 attempt["content"] = content 
# L266 |                 valid_media_list.append(attempt)
# L267 |                 break
# L268 | 
# L269 |     unique_media = []
# L270 |     seen_urls = set()
# L271 |     for m in valid_media_list:
# L272 |         if m["url"] not in seen_urls:
# L273 |             unique_media.append(m)
# L274 |             seen_urls.add(m["url"])
# L275 | 
# L276 |     caption = f"<b>【{board_name}】</b>\n#{post_id} | {posted_at}\n\n{body_text[:400]}"
# L277 |     keyboard = {"inline_keyboard": [[{"text": "Site", "url": board_url}, {"text": "Original", "url": target_post_url}]]}
# L278 | 
# L279 |     if not unique_media:
# L280 |         requests.post(
# L281 |             f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
# L282 |             data={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
# L283 |         )
# L284 |         return
# L285 | 
# L286 |     for media in unique_media:
# L287 |         method = "sendVideo" if media["type"] == "video" else "sendPhoto"
# L288 |         file_content = media.get("content") or fetch_content_with_retry(media["url"], timeout=45, retries=5)
# L289 |         
# L290 |         if file_content:
# L291 |             try:
# L292 |                 files = {("video" if media["type"] == "video" else "photo"): (f"file.{media['ext']}", file_content)}
# L293 |                 tel_res = requests.post(
# L294 |                     f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
# L295 |                     data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)},
# L296 |                     files=files,
# L297 |                     timeout=60
# L298 |                 )
# L299 |                 if tel_res.status_code != 200:
# L300 |                     print(f"      [ERROR] Telegram API failed: {tel_res.text}")
# L301 |             except Exception as e:
# L302 |                 print(f"      [ERROR] Exception during Telegram send: {e}")
# L303 |         else:
# L304 |             print(f"      [ERROR] All download attempts failed for: {media['url']}")
# L305 | 
# L306 | # ===== 処理実行ループ =====
# L307 | for index, target in enumerate(url_list, start=1):
# L308 |     board_id = get_board_id(target, index)
# L309 | 
# L310 |     soup = fetch_page_soup(target)
# L311 |     if not soup:
# L312 |         print(f" [ERROR] Target {board_id} failed to load.")
# L313 |         continue
# L314 | 
# L315 |     board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
# L316 |     print(f"--- Checking: {board_id} ---")
# L317 |     
# L318 |     articles = soup.select("article.resentry")
# L319 |     
# L320 |     saved_max_id, last_ids_list = load_last_post_ids_ab(board_id)
# L321 |     new_max_id = saved_max_id
# L322 |     current_batch_ids = []
# L323 | 
# L324 |     for article in reversed(articles):
# L325 |         try:
# L326 |             eno_text = article.select_one("span.eno a").get_text(strip=True)
# L327 |             post_id = int(re.search(r'\d+', eno_text).group())
# L328 |         except: continue
# L329 |         
# L330 |         if saved_max_id is not None and post_id <= saved_max_id:
# L331 |             continue
# L332 |         
# L333 |         if post_id in last_ids_list:
# L334 |             if saved_max_id is not None and post_id > saved_max_id: pass
# L335 |             else: continue
# L336 | 
# L337 |         if post_id in sent_post_ids: continue
# L338 |         
# L339 |         if saved_max_id is None:
# L340 |             current_batch_ids.append(post_id)
# L341 |             new_max_id = max(new_max_id or 0, post_id)
# L342 |             continue
# L343 | 
# L344 |         print(f"  -> [NEW] Item #{post_id} detected.")
# L345 |         posted_at = article.select_one("time.date").get_text(strip=True) if article.select_one("time.date") else "N/A"
# L346 |         body_text = article.select_one("div.comment").get_text("\n", strip=True) if article.select_one("div.comment") else ""
# L347 |         
# L348 |         media_urls = [urljoin(target, a["href"]) for a in article.select(".filethumblist li a[href]")]
# L349 |         extracted = extract_urls(body_text)
# L350 |         
# L351 |         if extracted or media_urls:
# L352 |             media_urls.extend(extracted)
# L353 |             send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, target, f"{target}/{post_id}", list(set(media_urls)))
# L354 |             sent_post_ids.add(post_id)
# L355 |         
# L356 |         current_batch_ids.append(post_id)
# L357 |         new_max_id = max(new_max_id or 0, post_id)
# L358 | 
# L359 |     if current_batch_ids:
# L360 |         save_last_post_ids_local_ab(board_id, new_max_id, current_batch_ids)
# L361 |     else:
# L362 |         print(" [LOG] No new items.")
# L363 | 
# L364 | commit_and_push_all()
