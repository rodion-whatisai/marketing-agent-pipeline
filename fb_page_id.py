"""
TNC — Facebook Page ID Finder v2
==================================
Находит ВСЕ Facebook аккаунты на сайте,
проверяет живые/битые, получает Page ID для каждого,
строит Ads Library ссылки.

Запуск:
    python fb_page_id.py bandago.com
    python fb_page_id.py hipcamp.com
    python fb_page_id.py BandagoHQ          # напрямую по handle
"""

import sys
import re
import json
import time
import requests
from urllib.parse import urlparse

import os
from pathlib import Path

from utils import get_scan_dir, scan_path, HEADERS





# Суффиксы региональных аккаунтов
COUNTRY_SUFFIXES = [
    "canada", "uk", "australia", "au", "gb", "ca", "us", "fr", "de",
    "es", "it", "nl", "br", "mx", "in", "jp", "kr", "sg", "nz",
    "ie", "za", "ng", "ke", "ph", "id", "th", "vn", "my",
]

# Паттерны для числового Page ID
PAGE_ID_PATTERNS = [
    r'"pageID"\s*:\s*"(\d{10,})"',
    r'"page_id"\s*:\s*"(\d{10,})"',
    r'"ownerId"\s*:\s*"(\d{10,})"',
    r'"profileID"\s*:\s*"(\d{10,})"',
    r'"userID"\s*:\s*"(\d{10,})"',
    r'fb://profile/(\d{10,})',
    r'content="fb://page/(\d{10,})"',
    r'entity_id=(\d{10,})',
    r'"entityID"\s*:\s*"(\d{10,})"',
    r'"nid"\s*:\s*(\d{10,})',
    r'"id"\s*:\s*"(\d{10,})"',
]

SKIP_FB_PATHS = {
    "sharer", "share", "tr", "dialog", "plugins", "photo", "video",
    "events", "groups", "pages", "help", "privacy", "legal", "ads",
    "business", "policies", "about", "login", "watch", "marketplace",
    "gaming", "fundraisers", "messenger",
}


# ─── Сбор всех FB ссылок на сайте ────────────────────────────────────────────

def find_all_fb_handles(base_url: str) -> list:
    """Находит все уникальные Facebook аккаунты на сайте. Поддерживает все форматы URL."""
    found = {}  # key → {handle, url, page_id}

    try:
        r = requests.get(base_url, headers=HEADERS, timeout=10)
        html = r.text

        # Формат 1: facebook.com/vanityname
        for handle in re.findall(
            r'https?://(?:www\.)?facebook\.com/([a-zA-Z0-9._-]+)/?(?:["\'\s]|$)',
            html
        ):
            handle = handle.strip("/").lower()
            if handle in SKIP_FB_PATHS or len(handle) < 3 or "?" in handle:
                continue
            key = f"handle:{handle}"
            if key not in found:
                found[key] = {
                    "handle": handle,
                    "url": f"https://www.facebook.com/{handle}",
                    "page_id": None,
                    "format": "vanity",
                }

        # Формат 2: facebook.com/people/Name/ID/
        for name, page_id in re.findall(
            r'facebook\.com/people/([^/"\']+)/(\d{10,})/?',
            html
        ):
            key = f"people:{page_id}"
            if key not in found:
                found[key] = {
                    "handle": name.lower().replace("-", "_"),
                    "url": f"https://www.facebook.com/people/{name}/{page_id}/",
                    "page_id": page_id,
                    "format": "people",
                    "display_name": name.replace("-", " "),
                }

        # Формат 3: facebook.com/profile.php?id=ID
        for page_id in re.findall(
            r'facebook\.com/profile\.php\?id=(\d{10,})',
            html
        ):
            key = f"profile:{page_id}"
            if key not in found:
                found[key] = {
                    "handle": f"profile_{page_id}",
                    "url": f"https://www.facebook.com/profile.php?id={page_id}",
                    "page_id": page_id,
                    "format": "profile",
                }

        # Формат 4: facebook.com/pages/Name/ID
        for name, page_id in re.findall(
            r'facebook\.com/pages/([^/"\']+)/(\d{10,})/?',
            html
        ):
            key = f"pages:{page_id}"
            if key not in found:
                found[key] = {
                    "handle": name.lower(),
                    "url": f"https://www.facebook.com/pages/{name}/{page_id}/",
                    "page_id": page_id,
                    "format": "pages",
                }

    except Exception as e:
        print(f"  ⚠️  Ошибка при загрузке сайта: {e}")

    return list(found.values())



def prioritize_handles(handles: list, brand_name: str) -> list:
    """
    Сортирует handle по приоритету:
    1. Точное совпадение с брендом
    2. Содержит название бренда, нет региональных суффиксов
    3. Региональные аккаунты
    """
    brand = brand_name.lower().replace("-", "").replace(".", "")

    def score(item):
        h = item["handle"].lower().replace("-", "").replace(".", "")
        # Точное совпадение
        if h == brand:
            return 0
        # Содержит бренд, нет суффиксов
        if brand in h:
            has_suffix = any(h.endswith(s) or h.startswith(s) for s in COUNTRY_SUFFIXES)
            return 1 if not has_suffix else 2
        return 3

    return sorted(handles, key=score)


# ─── Проверка живой/битой ссылки ──────────────────────────────────────────────

def check_fb_page_alive(handle: str) -> dict:
    """Проверяет существует ли Facebook страница."""
    url = f"https://www.facebook.com/{handle}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        html = r.text

        # Признаки битой страницы — только системные сообщения, не контент постов
        dead_signals = [
            "this page isn't available",
            "the link you followed may be broken",
            "this page has been removed",
            "sorry, this page isn't available",
        ]

        html_lower = html.lower()
        for signal in dead_signals:
            if signal in html_lower:
                return {"alive": False, "reason": signal}

        # Если редирект на login — страница защищена но существует
        if "login" in r.url and r.url != url:
            return {"alive": True, "reason": "redirected_to_login"}

        # Статус 404
        if r.status_code == 404:
            return {"alive": False, "reason": "404"}

        return {"alive": True, "reason": "ok"}

    except Exception as e:
        return {"alive": False, "reason": str(e)[:50]}


# ─── Получение Page ID ────────────────────────────────────────────────────────

def check_fb_page_alive_playwright(handle: str) -> dict:
    """Проверяет страницу через браузер. Заодно вытаскивает display name."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()
            try:
                page.goto(f"https://www.facebook.com/{handle}",
                         wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

            html_raw = page.content()
            html = html_raw.lower()

            # Вытаскиваем display name из <title> или h1
            display_name = None
            title_m = re.search(r"<title>([^|<]+)", html_raw)
            if title_m:
                candidate = title_m.group(1).strip()
                if candidate and "facebook" not in candidate.lower() and len(candidate) > 1:
                    display_name = candidate

            if not display_name:
                h1_m = re.search(r'<h1[^>]*>([^<]+)</h1>', html_raw)
                if h1_m:
                    display_name = h1_m.group(1).strip()

            browser.close()

            dead_signals = [
                "this page isn't available",
                "the link you followed may be broken",
                "this page has been removed",
                "sorry, this page isn't available",
            ]
            for signal in dead_signals:
                if signal in html:
                    return {"alive": False, "reason": f"playwright: {signal[:40]}"}

            return {"alive": True, "reason": "playwright_ok", "display_name": display_name}
    except Exception:
        return {"alive": True, "reason": "playwright_failed_assuming_alive", "display_name": None}


def get_page_id_requests(handle: str) -> str | None:
    """Пробует получить Page ID через requests (быстро)."""
    # Graph API без токена
    try:
        r = requests.get(
            f"https://graph.facebook.com/{handle}?fields=id,name",
            headers=HEADERS, timeout=8
        )
        if r.status_code == 200:
            data = r.json()
            if "id" in data and len(data["id"]) >= 10:
                return data["id"]
    except Exception:
        pass

    # mbasic
    try:
        r = requests.get(
            f"https://mbasic.facebook.com/{handle}",
            headers=HEADERS, timeout=10
        )
        html = r.text
        for pattern in PAGE_ID_PATTERNS:
            matches = re.findall(pattern, html)
            if matches:
                from collections import Counter
                counts = Counter(matches)
                best = counts.most_common(1)[0][0]
                if len(best) >= 10:
                    return best
    except Exception:
        pass

    return None


def get_page_id_playwright(handle: str) -> str | None:
    """Открывает Facebook через браузер и ищет Page ID."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()
            page_ids = []

            def on_response(response):
                try:
                    if "facebook.com" in response.url and response.status == 200:
                        ct = response.headers.get("content-type", "")
                        if any(t in ct for t in ["json", "javascript", "html"]):
                            try:
                                body = response.body().decode("utf-8", errors="ignore")
                                for pattern in PAGE_ID_PATTERNS:
                                    for m in re.findall(pattern, body):
                                        if len(m) >= 10:
                                            page_ids.append(m)
                            except Exception:
                                pass
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(f"https://www.facebook.com/{handle}",
                         wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(2000)
            except Exception:
                try:
                    page.goto(f"https://www.facebook.com/{handle}",
                             wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

            # Финальный HTML
            try:
                html = page.content()
                for pattern in PAGE_ID_PATTERNS:
                    for m in re.findall(pattern, html):
                        if len(m) >= 10:
                            page_ids.append(m)
            except Exception:
                pass

            browser.close()

            if page_ids:
                from collections import Counter
                counts = Counter(page_ids)
                return counts.most_common(1)[0][0]

    except Exception:
        pass

    return None


def get_page_id(handle: str) -> tuple:
    """Пробует все методы, возвращает (page_id, method)."""
    # Быстрые методы сначала
    pid = get_page_id_requests(handle)
    if pid:
        return pid, "requests"

    # Playwright как fallback
    pid = get_page_id_playwright(handle)
    if pid:
        return pid, "playwright"

    return None, None


# ─── Ads Library URLs ─────────────────────────────────────────────────────────

def build_ads_library_urls(display_name: str, countries: list = None) -> dict:
    """Строит ссылки на Ads Library через keyword search по display name.
    view_all_page_id не используется — не работает без логина.
    """
    if countries is None:
        countries = ["ALL", "CA", "US", "GB", "AU"]

    keyword = display_name.strip()
    urls = {}
    for country in countries:
        base = (
            f"https://www.facebook.com/ads/library/"
            f"?ad_type=all&country={country}"
            f"&media_type=all&search_type=keyword_unordered"
            f"&sort_data[mode]=total_impressions&sort_data[direction]=desc"
            f"&q={keyword}"
        )
        urls[country] = {
            "active_only": base + "&active_status=active",
            "all": base + "&active_status=all",
        }
    return urls


# ─── Main ─────────────────────────────────────────────────────────────────────

def get_active_ads_count(display_name: str) -> dict:
    """
    Проверяет наличие активной рекламы через keyword search в Ads Library.
    Работает без логина. view_all_page_id не используется.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"count": None, "error": "playwright not installed"}

    keyword = display_name.strip()
    url = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country=ALL"
        f"&media_type=all&search_type=keyword_unordered&q={keyword}"
    )

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()

            try:
                page.goto(url, wait_until="networkidle", timeout=25000)
                page.wait_for_timeout(3000)
            except Exception:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    page.wait_for_timeout(4000)
                except Exception:
                    pass

            html = page.content()
            browser.close()

        # Паттерн 1: JSON count
        json_count = re.search(
            r'"search_results_connection"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)',
            html
        )
        if json_count:
            count = int(json_count.group(1))
            return {"count": count, "status": "active" if count > 0 else "no_active_ads", "method": "json"}

        # Паттерн 2: "~N results" heading
        heading = re.search(r'~?(\d[\d,\s]*)\s+results?', html, re.IGNORECASE)
        if heading:
            num_str = re.sub(r'[,\s]', '', heading.group(1))
            try:
                count = int(num_str)
                if 0 < count < 1000000:
                    return {"count": count, "status": "active", "method": "heading"}
            except ValueError:
                pass

        # Паттерн 3: пустой результат
        no_results_signals = [
            'no ads match',
            'no results',
            '"edges":[]',
            '"count":0',
        ]
        if any(s in html.lower() for s in no_results_signals):
            return {"count": 0, "status": "no_active_ads", "method": "empty_signal"}

        return {"count": None, "status": "could_not_parse"}

    except Exception as e:
        return {"count": None, "error": str(e)[:100]}

def run(target: str) -> dict:
    print(f"\n{'═' * 60}")
    print(f"  Facebook Accounts Finder v2")
    print(f"  Target: {target}")
    print(f"{'═' * 60}")

    # Определяем что передали
    is_domain = "." in target and not target.startswith("http://www.facebook")
    is_handle = not is_domain and "facebook.com" not in target

    if is_handle:
        # Прямой handle — обрабатываем как одиночный
        handle = target.strip("/").split("facebook.com/")[-1].split("?")[0]
        handles = [{"handle": handle, "url": f"https://www.facebook.com/{handle}"}]
        brand_name = handle
    else:
        # Домен — собираем все FB ссылки
        base_url = ("https://" + target if not target.startswith("http") else target).rstrip("/")
        brand_name = urlparse(base_url).netloc.split(".")[0]

        print(f"\n🔍 Ищу Facebook аккаунты на {target}...")
        handles = find_all_fb_handles(base_url)

        if not handles:
            print(f"  ❌ Facebook ссылки не найдены")
            return {}

        print(f"  ✓ Найдено ссылок: {len(handles)}")
        handles = prioritize_handles(handles, brand_name)

    # Обрабатываем каждый аккаунт
    results = []
    print(f"\n{'─' * 60}")

    for item in handles:
        handle = item["handle"]
        fmt = item.get("format", "vanity")
        display = item.get("display_name", f"@{handle}")
        print(f"\n  {display} [{fmt}]")

        # Playwright как основной метод проверки
        # Для /people/ и /profile/ форматов — проверяем по полному URL
        check_handle = handle
        if fmt in ("people", "profile", "pages"):
            # Используем URL напрямую через Playwright
            status = check_fb_page_alive_playwright(item["url"].replace("https://www.facebook.com/", ""))
        else:
            status = check_fb_page_alive_playwright(handle)

        if not status["alive"]:
            print(f"    ✗ DEAD LINK — {status['reason']}")
            results.append({
                "handle": handle,
                "url": item["url"],
                "published": False,
                "broken_reason": status["reason"],
                "page_id": None,
                "ads_library": None,
            })
            continue

        # Display name — из Playwright результата (уже есть в status)
        display_name = status.get("display_name") or display

        print(f"    ✓ Published | Display name: {display_name}")

        # Ads Library — всё через keyword search по display name
        ads_urls = build_ads_library_urls(display_name)
        print(f"    📢 Ads Library: {ads_urls['ALL']['active_only']}")

        # Проверяем активную рекламу
        print(f"    🔢 Проверяю рекламу...")
        ads_count = get_active_ads_count(display_name)
        if ads_count.get("count") is not None:
            count = ads_count["count"]
            if count == 0:
                print(f"    ❌ Активных объявлений: 0")
            else:
                print(f"    ✅ Активных объявлений: ~{count}")
        else:
            print(f"    ⚠️  Не удалось определить количество")

        results.append({
            "handle": handle,
            "display_name": display_name,
            "url": item["url"],
            "alive": True,
            "active_ads_count": ads_count.get("count"),
            "ads_library": ads_urls,
        })

        time.sleep(0.5)  # небольшая пауза между запросами

    # Summary
    print(f"\n{'═' * 60}")
    print(f"  ИТОГО")
    print(f"{'═' * 60}")
    alive = [r for r in results if r["alive"]]
    broken = [r for r in results if not r["alive"]]
    print(f"  Published аккаунтов:    {len(alive)}")
    if broken:
        print(f"\n  ⚠️  DEAD LINKS ON SITE ({len(broken)}):")
        for r in broken:
            print(f"    • facebook.com/{r['handle']}")

    # Сохраняем
    output = {
        "target": target,
        "brand_name": brand_name,
        "accounts": results,
    }

    filename = scan_path(brand_name, "fb.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n💾 Сохранено: {filename}")

    return output


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python fb_page_id.py bandago.com")
        print("  python fb_page_id.py hipcamp.com")
        print("  python fb_page_id.py BandagoHQ")
        sys.exit(1)

    run(sys.argv[1])
