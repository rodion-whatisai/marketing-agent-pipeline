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





# ─── Определение страны сайта ────────────────────────────────────────────────

TLD_TO_COUNTRY = {
    "ca": "CA", "co.uk": "GB", "uk": "GB", "com.au": "AU", "au": "AU",
    "co.nz": "NZ", "nz": "NZ", "ie": "IE", "de": "DE", "fr": "FR",
    "es": "ES", "it": "IT", "nl": "NL", "br": "BR", "mx": "MX",
    "in": "IN", "jp": "JP", "sg": "SG", "za": "ZA",
}

LANG_TAG_TO_COUNTRY = {
    "en-ca": "CA", "fr-ca": "CA",
    "en-gb": "GB", "en-au": "AU", "en-nz": "NZ", "en-ie": "IE",
    "en-za": "ZA", "en-sg": "SG",
    "de": "DE", "de-de": "DE", "de-at": "AT", "de-ch": "CH",
    "fr": "FR", "fr-fr": "FR", "fr-be": "BE",
    "nl": "NL", "nl-nl": "NL", "nl-be": "BE",
    "es": "ES", "es-es": "ES", "pt-br": "BR",
    "it": "IT", "it-it": "IT",
    "ja": "JP", "ja-jp": "JP",
    "ko": "KR", "ko-kr": "KR",
    "zh-cn": "CN", "zh-tw": "TW",
}


def detect_site_country(domain: str, html: str = "", response_headers: dict = None) -> dict:
    """
    Определяет страну сайта по трём сигналам в порядке приоритета:
      1. Content-Language header (en-CA, fr-FR…)
      2. HTML lang attribute (en-CA, nl, de…)
      3. TLD домена (.ca, .de, .nl…)
    Если ни один не сработал — дефолт ALL.

    Возвращает {"country": "CA", "source": "content-language-header"}
    """
    # 1. Content-Language header
    if response_headers:
        cl = response_headers.get("Content-Language", response_headers.get("content-language", ""))
        if cl:
            tag = cl.strip().split(",")[0].strip().lower()
            if tag in LANG_TAG_TO_COUNTRY:
                return {"country": LANG_TAG_TO_COUNTRY[tag], "source": f"content-language-header ({cl.strip()})"}
            # Попробуем без региона (de → DE)
            base = tag.split("-")[0]
            if base in LANG_TAG_TO_COUNTRY:
                return {"country": LANG_TAG_TO_COUNTRY[base], "source": f"content-language-header ({cl.strip()})"}

    # 2. HTML lang attribute
    if html:
        import re as _re
        m = _re.search(r'<html[^>]+lang=["\']([a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?)["\']', html, _re.IGNORECASE)
        if m:
            tag = m.group(1).lower()
            if tag in LANG_TAG_TO_COUNTRY:
                return {"country": LANG_TAG_TO_COUNTRY[tag], "source": f"html-lang-attr ({m.group(1)})"}
            base = tag.split("-")[0]
            if base in LANG_TAG_TO_COUNTRY:
                return {"country": LANG_TAG_TO_COUNTRY[base], "source": f"html-lang-attr ({m.group(1)})"}

    # 3. TLD
    d = domain.lower().rstrip("/").split("?")[0]
    for tld, country in TLD_TO_COUNTRY.items():
        if d.endswith(f".{tld}"):
            return {"country": country, "source": f"tld (.{tld})"}

    return {"country": "ALL", "source": "no signals found — defaulting to ALL"}


# Суффиксы региональных аккаунтов
COUNTRY_SUFFIXES = [
    "canada", "uk", "australia", "au", "gb", "ca", "us", "fr", "de",
    "es", "it", "nl", "br", "mx", "in", "jp", "kr", "sg", "nz",
    "ie", "za", "ng", "ke", "ph", "id", "th", "vn", "my",
]

# Паттерны для числового Page ID
PAGE_ID_PATTERNS = [
    # Самый надёжный — userID рядом с userVanity (это точно page/profile ID)
    r'"userID"\s*:\s*"(\d{10,})".*?"userVanity"',
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
    """Проверяет страницу через браузер. Заодно вытаскивает display name и page_id."""
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

            # Собираем page_id из network responses
            page_ids_found = []

            def on_response(response):
                try:
                    if "facebook.com" in response.url and response.status == 200:
                        ct = response.headers.get("content-type", "")
                        if any(t in ct for t in ["json", "javascript", "html"]):
                            body = response.body().decode("utf-8", errors="ignore")
                            for pattern in PAGE_ID_PATTERNS:
                                for m in re.findall(pattern, body):
                                    # findall может вернуть tuple если есть группы
                                    m = m[0] if isinstance(m, tuple) else m
                                    if len(m) >= 10:
                                        page_ids_found.append(m)
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(f"https://www.facebook.com/{handle}",
                         wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

            html_raw = page.content()
            html = html_raw.lower()

            # Ищем page_id в финальном HTML тоже
            for pattern in PAGE_ID_PATTERNS:
                for m in re.findall(pattern, html_raw):
                    m = m[0] if isinstance(m, tuple) else m
                    if len(m) >= 10:
                        page_ids_found.append(m)

            browser.close()

            # Вытаскиваем display name
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

            # Берём самый частый page_id
            page_id = None
            if page_ids_found:
                from collections import Counter
                page_id = Counter(page_ids_found).most_common(1)[0][0]

            dead_signals = [
                "this page isn't available",
                "the link you followed may be broken",
                "this page has been removed",
                "sorry, this page isn't available",
            ]
            for signal in dead_signals:
                if signal in html:
                    return {"alive": False, "reason": f"playwright: {signal[:40]}"}

            return {"alive": True, "reason": "playwright_ok", "display_name": display_name, "page_id": page_id}
    except Exception:
        return {"alive": True, "reason": "playwright_failed_assuming_alive", "display_name": None, "page_id": None}


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

def build_ads_library_urls(display_name: str, countries: list = None, page_id: str = None) -> dict:
    """Строит ссылки на Ads Library — keyword search по display name, country=ALL."""
    keyword = display_name.strip().replace(" ", "%20")
    base = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country=ALL"
        f"&is_targeted_country=false&media_type=all"
        f"&q={keyword}"
        f"&search_type=keyword_unordered"
        f"&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )
    return {
        "ALL": {
            "active_only": base,
            "all": base.replace("active_status=active", "active_status=all"),
        }
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def get_active_ads_count(display_name: str, page_id: str = None,
                         country: str = "ALL", fb_page_url: str = None) -> dict:
    """
    Проверяет наличие активной рекламы по display name (keyword search).
    Возвращает count + search_term для прозрачности.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"count": None, "error": "playwright not installed"}

    keyword = display_name.strip().replace(" ", "%20")
    url = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country={country}"
        f"&is_targeted_country=false&media_type=all"
        f"&q={keyword}"
        f"&search_type=keyword_unordered"
        f"&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )
    search_meta = {"search_term": display_name, "country": country}

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
            r'"search_results_connection"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)', html
        )
        if json_count:
            count = int(json_count.group(1))
            return {"count": count, "status": "active" if count > 0 else "no_active_ads",
                    "method": "json", **search_meta}

        # Паттерн 2: "~N results"
        heading = re.search(r'~?(\d[\d,\s]*)\s+results?', html, re.IGNORECASE)
        if heading:
            num_str = re.sub(r'[,\s]', '', heading.group(1))
            try:
                count = int(num_str)
                if 0 < count < 1000000:
                    return {"count": count, "status": "active",
                            "method": "heading", **search_meta}
            except ValueError:
                pass

        # Паттерн 3: пустой результат
        if any(s in html.lower() for s in ['no ads match', 'no results', '"edges":[]', '"count":0']):
            return {"count": 0, "status": "no_active_ads", "method": "empty_signal", **search_meta}

        return {"count": None, "status": "could_not_parse", **search_meta}

    except Exception as e:
        return {"count": None, "error": str(e)[:100], **search_meta}

def run(target: str) -> dict:
    print(f"\n{'═' * 60}")
    print(f"  Facebook Accounts Finder v2")
    print(f"  Target: {target}")
    print(f"{'═' * 60}")

    # Определяем что передали
    is_domain = "." in target and not target.startswith("http://www.facebook")
    is_handle = not is_domain and "facebook.com" not in target

    if is_handle:
        handle = target.strip("/").split("facebook.com/")[-1].split("?")[0]
        handles = [{"handle": handle, "url": f"https://www.facebook.com/{handle}"}]
        brand_name = handle
        site_country = "ALL"
        site_country_source = "no domain — defaulting to ALL"
    else:
        base_url = ("https://" + target if not target.startswith("http") else target).rstrip("/")
        brand_name = urlparse(base_url).netloc.split(".")[0]

        _html, _headers = "", {}
        try:
            _r = requests.get(base_url, headers=HEADERS, timeout=10)
            _html = _r.text
            _headers = dict(_r.headers)
        except Exception:
            pass

        country_result = detect_site_country(urlparse(base_url).netloc, html=_html, response_headers=_headers)
        site_country = country_result["country"]
        site_country_source = country_result["source"]

        print(f"\n🌍 Страна сайта: {site_country}  (via {site_country_source})")
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
        fb_page_url = item["url"]
        print(f"\n  {display} [{fmt}]")

        if fmt in ("people", "profile", "pages"):
            status = check_fb_page_alive_playwright(item["url"].replace("https://www.facebook.com/", ""))
        else:
            status = check_fb_page_alive_playwright(handle)

        if not status["alive"]:
            print(f"    ✗ DEAD LINK — {status['reason']}")
            results.append({
                "handle": handle,
                "url": fb_page_url,
                "published": False,
                "broken_reason": status["reason"],
                "page_id": None,
                "ads_library": None,
            })
            continue

        display_name = status.get("display_name") or display

        print(f"    ✓ Published | Display name: {display_name}")

        ads_urls = build_ads_library_urls(display_name)
        print(f"    📢 Ads Library: {ads_urls['ALL']['active_only']}")

        print(f"    🔢 Проверяю рекламу по имени '{display_name}'...")
        ads_count = get_active_ads_count(display_name, country="ALL", fb_page_url=fb_page_url)

        count = ads_count.get("count")

        if count is not None:
            status_icon = "✅" if count > 0 else "❌"
            print(f"    {status_icon} Активных объявлений: {'~' if count > 0 else ''}{count}")
        else:
            print(f"    ⚠️  Не удалось определить количество")

        results.append({
            "handle": handle,
            "display_name": display_name,
            "url": fb_page_url,
            "alive": True,
            "active_ads_count": count,
            "ads_search_term": display_name,
            "ads_library": ads_urls,
        })

        time.sleep(0.5)

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
        "site_country": site_country,
        "site_country_source": site_country_source,
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
