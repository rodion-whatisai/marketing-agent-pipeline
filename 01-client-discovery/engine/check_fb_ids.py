import requests

ids_to_check = [
    "100064800065603",
    "1178003164369675",
    "119905691356787",
    "101928235310512",
    "124888454241574",  # тот что работал в браузере
]

headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"}

for page_id in ids_to_check:
    try:
        # Graph API без токена — возвращает публичное имя
        r = requests.get(
            f"https://graph.facebook.com/{page_id}?fields=name,category",
            headers=headers, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            print(f"  {page_id}: {data.get('name', '?')} [{data.get('category', '?')}]")
        else:
            print(f"  {page_id}: HTTP {r.status_code}")
    except Exception as e:
        print(f"  {page_id}: ERROR {e}")
