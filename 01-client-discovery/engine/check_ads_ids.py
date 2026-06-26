import requests

from log import log_info, log_error, log_debug

ids_to_check = [
    "100064800065603",
    "1178003164369675",
    "119905691356787",
    "101928235310512",
    "124888454241574",
]

headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

log_debug(f"Проверяем {len(ids_to_check)} page_id через FB Ads Library view_all_page_id")

for page_id in ids_to_check:
    log_debug(f"page_id={page_id}: строим URL запроса")
    url = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&media_type=all&search_type=page&view_all_page_id={page_id}"
    try:
        log_debug(f"page_id={page_id}: GET {url}")
        r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        log_debug(f"page_id={page_id}: ответ получен status={r.status_code} len={len(r.text)}")
        # Смотрим куда редиректнуло и есть ли результаты
        has_ads = "ad_archive_id" in r.text or "results" in r.text.lower()
        redirected = r.url != url
        log_debug(f"page_id={page_id}: has_ads={has_ads} redirected={redirected}")
        log_info(f"  {page_id}: status={r.status_code} redirected={redirected} has_ads_content={has_ads}")
        log_info(f"    final_url: {r.url[:100]}")
    except Exception as e:
        log_error(f"  {page_id}: ERROR {e}")
