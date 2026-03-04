001: import os
002: import sys
003: import re
004: import time
005: import json
006: import io
007: import subprocess
008: import hashlib # IDをハッシュ化するための標準ライブラリ
009: from urllib.parse import urljoin, urlparse
010: 
011: import requests
012: from bs4 import BeautifulSoup
013: 
014: # ===== 設定スイッチ =====
015: LOG_WITH_TITLE = False 
016: 
017: # ===== 定数・環境変数 =====
018: TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
019: TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
020: TARGET_URL = os.environ.get("TARGET_URL")
021: 
022: # --- 秘匿設定 ---
023: DOMAIN_SUFFIX = os.environ.get("DOMAIN_SUFFIX", "") 
024: EXTERNAL_DOMAINS = os.environ.get("EXTERNAL_DOMAINS", "").split(",") 
025: MEDIA_PREFIX = "cdn" 
026: # ----------------
027: 
028: if not all([TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TARGET_URL]):
029:     print("Missing environment variables.")
030:     sys.exit(1)
031: 
032: url_list = [u.strip() for u in TARGET_URL.split(",") if u.strip()]
033: headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
034: sent_post_ids = set()
035: URL_PATTERN = re.compile(r"https?://[\w/:%#\$&\?\(\)~\.=\+\-]+", re.IGNORECASE)
036: 
037: # 更新があったファイルを記録するリスト
038: updated_files = []
039: 
040: # ===== 状態管理 =====
041: def get_board_id(url: str, index: int) -> str:
042:     """URLを識別用の符号に変換します"""
043:     hashed = hashlib.md5(url.encode("utf-8")).hexdigest()[:12]
044:     return f"{index:02d}_{hashed}"
045: 
046: def load_last_post_ids_ab(board_id: str):
047:     """保存されたID情報を読み込みます"""
048:     fname = f"last_post_id_{board_id}.txt"
049:     if not os.path.exists(fname): return None, []
050:     try:
051:         with open(fname, "r", encoding="utf-8") as f:
052:             lines = f.read().strip().splitlines()
053:             if not lines: return None, []
054:             
055:             # 1行目：最大番号
056:             max_id = int(lines[0]) if lines[0].strip().isdigit() else None
057:             
058:             # 2行目：通知済みリスト
059:             id_list = []
060:             if len(lines) > 1:
061:                 id_list = [int(x) for x in lines[1].split(",") if x.strip().isdigit()]
062:             return max_id, id_list
063:     except: return None, []
064: 
065: def save_last_post_ids_local_ab(board_id: str, max_id: int, post_ids: list):
066:     """最新のID情報を保存します"""
067:     fname = f"last_post_id_{board_id}.txt"
068:     with open(fname, "w", encoding="utf-8") as f:
069:         f.write(f"{max_id}\n")
070:         f.write(",".join(map(str, sorted(post_ids))))
071:     if fname not in updated_files:
072:         updated_files.append(fname)
073: 
074: def commit_and_push_all():
075:     """IDファイルの更新を確定させます"""
076:     if not updated_files:
077:         print(" [LOG] No ID files to update.")
078:         return
079:     
080:     if os.environ.get("GITHUB_ACTIONS") == "true":
081:         try:
082:             subprocess.run(["git", "config", "user.name", "github-actions"], check=True)
083:             subprocess.run(["git", "config", "user.email", "github-actions@github.com"], check=True)
084:             for f in updated_files:
085:                 subprocess.run(["git", "add", f], check=True)
086:             
087:             status = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True)
088:             if status.stdout.strip():
089:                 subprocess.run(["git", "commit", "-m", "update files"], check=True)
090:                 subprocess.run(["git", "pull", "--rebase"], check=False)
091:                 subprocess.run(["git", "push"], check=True)
092:                 print(f" [LOG] Pushed {len(updated_files)} files.")
093:         except Exception as e:
094:             print(f" [ERROR] Push failed: {e}")
095: 
096: # ===== 通信ユーティリティ =====
097: 
098: def fetch_content_with_retry(url, timeout=30, retries=5):
099:     """
100:     ストリーミングを利用して大きなファイルを確実にダウンロードします。
101:     IncompleteReadエラーに対抗するため、Rangeヘッダーによるレジューム機能を搭載。
102:     """
103:     content = bytearray()
104:     
105:     for i in range(retries):
106:         try:
107:             current_headers = headers.copy()
108:             if len(content) > 0:
109:                 current_headers['Range'] = f"bytes={len(content)}-"
110:                 
111:             with requests.get(url, headers=current_headers, timeout=timeout, stream=True) as res:
112:                 # 200 or 206 OK
113:                 if res.status_code in [200, 206]:
114:                     if res.status_code == 200:
115:                         content = bytearray()
116:                         
117:                     for chunk in res.iter_content(chunk_size=16384): # 少しチャンクサイズを上げました
118:                         if chunk:
119:                             content.extend(chunk)
120:                     
121:                     # サイズ整合性チェック（Range未使用時のみ厳密にチェック）
122:                     expected_size = res.headers.get('Content-Length')
123:                     if expected_size and res.status_code == 200 and int(expected_size) != len(content):
124:                         raise requests.exceptions.ContentDecodingError("Incomplete download")
125:                     
126:                     return bytes(content)
127:                 
128:                 # 404などの場合はリトライせず終了
129:                 if res.status_code == 404:
130:                     return None
131:                     
132:                 print(f"      [WARN] HTTP {res.status_code} for {url} (Attempt {i+1}/{retries})")
133:                 
134:         except (requests.exceptions.RequestException, Exception) as e:
135:             wait_time = 2 ** (i + 1)
136:             print(f"      [WARN] Download error on {url}: {e} (Attempt {i+1}/{retries}). Retrying in {wait_time}s...")
137:             time.sleep(wait_time)
138:             
139:     return None
140: 
141: def fetch_page_soup(url, timeout=15):
142:     """ページ解析用にHTMLを取得してBeautifulSoupオブジェクトを返します"""
143:     try:
144:         res = requests.get(url, headers=headers, timeout=timeout)
145:         if res.status_code == 200:
146:             return BeautifulSoup(res.text, "html.parser")
147:     except: pass
148:     return None
149: 
150: # ===== 解析・送信ロジック =====
151: def extract_urls(text: str):
152:     """テキストからURLを抽出します"""
153:     found = URL_PATTERN.findall(text)
154:     unique_urls = sorted(list(set(found)))
155:     filtered_urls = []
156:     
157:     for url in unique_urls:
158:         if "/read.cgi/" in url:
159:             continue
160:         filtered_urls.append(url)
161:     return filtered_urls
162: 
163: def resolve_external_media(url, depth=0):
164:     """外部ページからメディアを抽出します（OGPメタデータ対応強化）"""
165:     if depth > 1:
166:         return None
167: 
168:     parsed_url = urlparse(url)
169:     # 対象ドメイン判定（EXTERNAL_DOMAINS または 5chan系）
170:     is_target = any(domain in url for domain in EXTERNAL_DOMAINS if domain) or "upup.be" in parsed_url.netloc or "5chan.jp" in parsed_url.netloc
171:     
172:     if is_target:
173:         soup = fetch_page_soup(url)
174:         if soup:
175:             try:
176:                 found_media_in_page = []
177:                 
178:                 # --- メタタグ (OGP) の解析を追加 (5chanのdispページ用) ---
179:                 og_video = soup.find("meta", property="og:video") or soup.find("meta", attrs={"name": "twitter:player:stream"})
180:                 if og_video and og_video.get("content"):
181:                     v_url = urljoin(url, og_video["content"])
182:                     ext = v_url.split(".")[-1].split("?")[0].lower()
183:                     found_media_in_page.append({"type": "video", "url": v_url, "ext": ext})
184: 
185:                 og_image = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
186:                 if og_image and og_image.get("content"):
187:                     i_url = urljoin(url, og_image["content"])
188:                     ext = i_url.split(".")[-1].split("?")[0].lower()
189:                     found_media_in_page.append({"type": "photo", "url": i_url, "ext": ext})
190: 
191:                 # --- 従来のタグ解析 ---
192:                 # videoタグ
193:                 video_tag = soup.find("video")
194:                 if video_tag:
195:                     src = video_tag.get("src") or (video_tag.find("source").get("src") if video_tag.find("source") else None)
196:                     if src:
197:                         full_v_url = urljoin(url, src)
198:                         ext = full_v_url.split(".")[-1].split("?")[0].lower()
199:                         m_obj = {"type": "video", "url": full_v_url, "ext": ext}
200:                         if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)
201: 
202:                 # aタグ (動画)
203:                 for a in soup.find_all("a", href=True):
204:                     href_lower = a["href"].lower()
205:                     if any(ext in href_lower for ext in [".mp4", ".mov", ".wmv", ".webm"]):
206:                         full_v_url = urljoin(url, a["href"])
207:                         ext = full_v_url.split(".")[-1].split("?")[0].lower()
208:                         m_obj = {"type": "video", "url": full_v_url, "ext": ext}
209:                         if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)
210: 
211:                 # 画像系 (動画がない、または画像も重要な場合)
212:                 for img in soup.find_all("img", src=True):
213:                     src = img.get("src")
214:                     if src and not any(x in src for x in ["qrcode", "logo", "icon"]):
215:                         full_img_url = urljoin(url, src)
216:                         ext = full_img_url.split(".")[-1].split("?")[0].lower()
217:                         if ext in ["jpg", "jpeg", "png", "gif", "webp"]:
218:                             m_obj = {"type": "photo", "url": full_img_url, "ext": ext}
219:                             if m_obj not in found_media_in_page: found_media_in_page.append(m_obj)
220: 
221:                 if depth == 1 and found_media_in_page:
222:                     return found_media_in_page
223: 
224:                 # 再帰解析 (dispから個別ページへなど)
225:                 if depth == 0:
226:                     base_id = parsed_url.path.strip('/').split('/')[-1] 
227:                     child_links = []
228:                     for a in soup.find_all("a", href=True):
229:                         full_child_url = urljoin(url, a["href"])
230:                         if parsed_url.netloc in full_child_url and base_id in full_child_url and full_child_url != url:
231:                             child_links.append(full_child_url)
232:                     
233:                     found_media_list = found_media_in_page
234:                     for child_url in set(child_links):
235:                         child_results = resolve_external_media(child_url, depth=1)
236:                         if child_results:
237:                             for r in child_results:
238:                                 if r not in found_media_list: found_media_list.append(r)
239:                     
240:                     return found_media_list
241:             except Exception as e:
242:                 print(f"      [ERROR] BS4 analysis failed for {url}: {e}")
243:     return None
244: 
245: def send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, board_url, target_post_url, media_urls):
246:     """解析結果をTelegramへ送信します"""
247:     print(f"      [LOG] Analyzing media for #{post_id}...")
248:     valid_media_list = []
249:     
250:     for m_url in media_urls:
251:         # 外部ページの解析
252:         external = resolve_external_media(m_url)
253:         if external:
254:             if isinstance(external, list): valid_media_list.extend(external)
255:             else: valid_media_list.append(external)
256:             continue
257: 
258:         # 特定ドメイン(5chan等)のURL直接生成ロジック
259:         parsed = urlparse(m_url)
260:         raw_file_id = parsed.path.rstrip("/").split("/")[-1]
261:         file_id = os.path.splitext(raw_file_id)[0] 
262:         
263:         netloc = parsed.netloc
264:         # 5chan系ドメインの変換 (e.5chan.jp -> cdne.5chan.jp)
265:         if DOMAIN_SUFFIX and DOMAIN_SUFFIX in netloc:
266:             if not netloc.startswith(MEDIA_PREFIX):
267:                 subdomain = netloc.split('.')[0]
268:                 netloc = f"{MEDIA_PREFIX}{subdomain}{DOMAIN_SUFFIX}"
269: 
270:         candidates = []
271:         # 動画候補
272:         for ext in ["mp4", "mov", "webm", "gif"]:
273:             candidates.append({"type": "video", "url": f"https://{netloc}/file/{file_id}.{ext}", "ext": ext})
274:         # 画像候補
275:         for ext in ["png", "jpg", "jpeg"]:
276:             candidates.append({"type": "photo", "url": f"https://{netloc}/file/plane/{file_id}.{ext}", "ext": ext})
277: 
278:         # 修正箇所: HEADは拒否されるため、直接取得を試みる（最初の成功した1つを採用）
279:         for attempt in candidates:
280:             # ここでは簡易的なチェックとして fetch_content_with_retry を使うが、
281:             # 候補が多い場合は最初の数KBだけ見て判断するように fetch_content_with_retry 内で処理
282:             # 現状はそのまま呼び出し
283:             content = fetch_content_with_retry(attempt["url"], timeout=10, retries=1)
284:             if content:
285:                 attempt["content"] = content # ダウンロード済みデータを保持
286:                 valid_media_list.append(attempt)
287:                 break
288: 
289:     unique_media = []
290:     seen_urls = set()
291:     for m in valid_media_list:
292:         if m["url"] not in seen_urls:
293:             unique_media.append(m)
294:             seen_urls.add(m["url"])
295: 
296:     caption = f"<b>【{board_name}】</b>\n#{post_id} | {posted_at}\n\n{body_text[:400]}"
297:     keyboard = {"inline_keyboard": [[{"text": "Site", "url": board_url}, {"text": "Original", "url": target_post_url}]]}
298: 
299:     if not unique_media:
300:         requests.post(
301:             f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
302:             data={"chat_id": TELEGRAM_CHAT_ID, "text": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)}
303:         )
304:         return
305: 
306:     for media in unique_media:
307:         method = "sendVideo" if media["type"] == "video" else "sendPhoto"
308:         # 既に content を持っている場合はそれを利用、なければ取得
309:         file_content = media.get("content") or fetch_content_with_retry(media["url"], timeout=45, retries=5)
310:         
311:         if file_content:
312:             try:
313:                 files = {("video" if media["type"] == "video" else "photo"): (f"file.{media['ext']}", file_content)}
314:                 tel_res = requests.post(
315:                     f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
316:                     data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "HTML", "reply_markup": json.dumps(keyboard)},
317:                     files=files,
318:                     timeout=60
319:                 )
320:                 if tel_res.status_code != 200:
321:                     print(f"      [ERROR] Telegram API failed: {tel_res.text}")
322:             except Exception as e:
323:                 print(f"      [ERROR] Exception during Telegram send: {e}")
324:         else:
325:             print(f"      [ERROR] All download attempts failed for: {media['url']}")
326: 
327: # ===== 処理実行ループ =====
328: for index, target in enumerate(url_list, start=1):
329:     board_id = get_board_id(target, index)
330: 
331:     soup = fetch_page_soup(target)
332:     if not soup:
333:         print(f" [ERROR] Target {board_id} failed to load.")
334:         continue
335: 
336:     board_name = soup.title.string.split("-")[0].strip() if soup.title else board_id
337:     print(f"--- Checking: {board_id} ---")
338:     
339:     articles = soup.select("article.resentry")
340:     
341:     saved_max_id, last_ids_list = load_last_post_ids_ab(board_id)
342:     new_max_id = saved_max_id
343:     current_batch_ids = []
344: 
345:     for article in reversed(articles):
346:         try:
347:             eno_text = article.select_one("span.eno a").get_text(strip=True)
348:             post_id = int(re.search(r'\d+', eno_text).group())
349:         except: continue
350:         
351:         if saved_max_id is not None and post_id <= saved_max_id:
352:             continue
353:         
354:         if post_id in last_ids_list:
355:             if saved_max_id is not None and post_id > saved_max_id: pass
356:             else: continue
357: 
358:         if post_id in sent_post_ids: continue
359:         
360:         if saved_max_id is None:
361:             current_batch_ids.append(post_id)
362:             new_max_id = max(new_max_id or 0, post_id)
363:             continue
364: 
365:         print(f"  -> [NEW] Item #{post_id} detected.")
366:         posted_at = article.select_one("time.date").get_text(strip=True) if article.select_one("time.date") else "N/A"
367:         body_text = article.select_one("div.comment").get_text("\n", strip=True) if article.select_one("div.comment") else ""
368:         
369:         media_urls = [urljoin(target, a["href"]) for a in article.select(".filethumblist li a[href]")]
370:         extracted = extract_urls(body_text)
371:         
372:         if extracted or media_urls:
373:             media_urls.extend(extracted)
374:             send_telegram_combined(board_name, board_id, post_id, posted_at, body_text, target, f"{target}/{post_id}", list(set(media_urls)))
375:             sent_post_ids.add(post_id)
376:         
377:         current_batch_ids.append(post_id)
378:         new_max_id = max(new_max_id or 0, post_id)
379: 
380:     if current_batch_ids:
381:         save_last_post_ids_local_ab(board_id, new_max_id, current_batch_ids)
382:     else:
383:         print(" [LOG] No new items.")
384: 
385: commit_and_push_all()
