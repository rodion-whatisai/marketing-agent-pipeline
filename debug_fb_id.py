import re
import sys
from collections import Counter
from playwright.sync_api import sync_playwright
from fb_page_id import PAGE_ID_PATTERNS

handle = sys.argv[1] if len(sys.argv) > 1 else "TheBodyShopUK"
print(f"Открываю facebook.com/{handle}...")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_context().new_page()
    page.goto(f"https://www.facebook.com/{handle}", wait_until="domcontentloaded", timeout=15000)
    page.wait_for_timeout(2000)
    html = page.content()
    browser.close()

print(f"HTML получен ({len(html)} символов)\n")

all_ids = []
for i, pattern in enumerate(PAGE_ID_PATTERNS):
    matches = re.findall(pattern, html)
    for m in matches:
        if len(m) >= 10:
            all_ids.append((m, i))

counts = Counter(m for m, _ in all_ids)
print("Все найденные ID (по частоте):")
for id_, cnt in counts.most_common(10):
    patterns_matched = [i for m, i in all_ids if m == id_]
    print(f"  {id_}  x{cnt}  via patterns: {patterns_matched}")
