import requests

ids_to_check = [
    "100064800065603",
    "1178003164369675", 
    "119905691356787",
    "101928235310512",
    "124888454241574",
]

headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

for page_id in ids_to_check:
    url = f"https://www.facebook.com/ads/library/?active_status=active&ad_type=all&country=ALL&media_type=all&search_type=page&view_all_page_id={page_id}"
    try:
        r = requests.get(url, headers=headers, timeout=10, allow_redirects=True)
        # Смотрим куда редиректнуло и есть ли результаты
        has_ads = "ad_archive_id" in r.text or "results" in r.text.lower()
        redirected = r.url != url
        print(f"  {page_id}: status={r.status_code} redirected={redirected} has_ads_content={has_ads}")
        print(f"    final_url: {r.url[:100]}")
    except Exception as e:
        print(f"  {page_id}: ERROR {e}")
