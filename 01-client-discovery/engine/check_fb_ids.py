import requests

from log import log_info, log_error, log_debug

ids_to_check = [
    "100064800065603",
    "1178003164369675",
    "119905691356787",
    "101928235310512",
    "124888454241574",  # тот что работал в браузере
]

headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

for page_id in ids_to_check:
    log_debug(f"проверяю page_id={page_id} через Graph API")
    try:
        # Graph API без токена — возвращает публичное имя
        log_debug(f"GET graph.facebook.com/{page_id}?fields=name,category (timeout=8)")
        r = requests.get(
            f"https://graph.facebook.com/{page_id}?fields=name,category",
            headers=headers, timeout=8
        )
        log_debug(f"{page_id}: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            log_debug(f"{page_id}: распарсил JSON ответ")
            log_info(f"  {page_id}: {data.get('name', '?')} [{data.get('category', '?')}]")
        else:
            log_info(f"  {page_id}: HTTP {r.status_code}")
    except Exception as e:
        log_error(f"  {page_id}: ERROR {e}")
