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

from utils import get_scan_dir, scan_path, HEADERS, setup_console
setup_console()





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


# ─── Homepage fetch with WAF fallback ────────────────────────────────────────

def fetch_homepage(base_url: str) -> tuple:
    """
    Пытается получить HTML домашней страницы. Сначала через requests (быстро),
    если блок (403/error) — через headless Playwright (обходит Cloudflare/WAF).

    Returns: (html, status_code, method, error)
      method ∈ {"requests", "playwright", "blocked_by_waf"}
    """
    # Step 1: requests
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.status_code < 400 and len(r.text) > 500:
            return r.text, r.status_code, "requests", None
        requests_status = r.status_code
    except Exception as e:
        requests_status = None
        _ = str(e)[:100]  # not used but kept for symmetry

    # Step 2: Playwright fallback
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", requests_status, "blocked_by_waf", "playwright not installed"

    print(f"    🎭 requests blocked ({requests_status}) — пробую Playwright...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            try:
                response = page.goto(base_url, wait_until="domcontentloaded", timeout=25000)
                # Дать JS догидратиться / cookie popup появиться (они не мешают — FB-линка
                # уже в footer HTML независимо от того, overlay ли popup поверх или нет)
                page.wait_for_timeout(3000)
                status = response.status if response else None
                html = page.content()
                browser.close()
                # Отказ если status 4xx/5xx — даже если есть HTML, это скорее всего
                # CF challenge page или error page, а не реальный контент сайта
                if html and len(html) > 500 and (status is None or status < 400):
                    return html, status, "playwright", None
                return "", status, "blocked_by_waf", f"playwright got status {status}"
            except Exception as e:
                browser.close()
                return "", requests_status, "blocked_by_waf", f"playwright error: {str(e)[:80]}"
    except Exception as e:
        return "", requests_status, "blocked_by_waf", f"playwright launch failed: {str(e)[:80]}"


# ─── Сбор всех FB ссылок на сайте ────────────────────────────────────────────

def find_all_fb_handles(base_url: str, html: str = None) -> list:
    """Находит все уникальные Facebook аккаунты на сайте. Поддерживает все форматы URL.

    Если html передан — используем его (избегая повторного fetch в случае если
    вызывающий уже fetched homepage через fetch_homepage).
    """
    found = {}  # key → {handle, url, page_id}

    if html is None:
        try:
            r = requests.get(base_url, headers=HEADERS, timeout=10)
            html = r.text
        except Exception as e:
            print(f"  ⚠️  Ошибка при загрузке сайта: {e}")
            return []

    # Формат 1: facebook.com/vanityname
    # Поддержка локалей (de-de.), мобильного (m.), web (web.), www
    # Lookahead — terminates at any URL boundary
    for handle in re.findall(
        r'https?://(?:[a-z]{2}-[a-z]{2}\.|www\.|m\.|web\.)?facebook\.com/'
        r'([a-zA-Z0-9._-]{3,})(?=[/?#"\'\s<>]|$)',
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

    # Формат 5: facebook.com/p/Name-with-dashes-NumericID/
    # Новый "Public Page URL" формат FB. Слаг и ID разделены последним дефисом.
    for name, page_id in re.findall(
        r'facebook\.com/p/([A-Za-z0-9._-]+?)-(\d{10,})/?',
        html
    ):
        key = f"p_path:{page_id}"
        if key not in found:
            found[key] = {
                "handle": f"p/{name}-{page_id}",
                "url": f"https://www.facebook.com/p/{name}-{page_id}/",
                "page_id": page_id,
                "format": "p_path",
                "display_name": name.replace("-", " "),
            }

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
    """Строит ссылки на Ads Library — keyword search.

    Даже если у нас есть page_id, мы НЕ используем view_all_page_id URL:
    в нашей практике он часто возвращает 0 результатов (возможно, из-за того,
    что playwright-extracted page_id не всегда соответствует тому что ждёт FB,
    или FB throttlит unauthenticated view_all_page_id запросы).

    Вместо этого — всегда keyword search, и результаты фильтруем пост-фактум
    по snapshot.page_id (exact) или snapshot.page_name (fuzzy match на difflib).
    См. _extract_ads_from_json. Параметр page_id здесь принимается для обратной
    совместимости, но игнорируется в URL.
    """
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
            "active_only":   base,
            "all":           base.replace("active_status=active", "active_status=all"),
            "inactive_only": base.replace("active_status=active", "active_status=inactive"),
        }
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def _walk_for_key(obj, key):
    """Рекурсивный обход dict/list — выдаёт все значения по заданному ключу."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _walk_for_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_for_key(v, key)


def _normalize_ad_record(raw: dict) -> dict:
    """Конвертит сырой GraphQL-ad record в плоский dict с нужными полями."""
    if not isinstance(raw, dict):
        return None
    try:
        s = raw.get("snapshot") or {}

        # Primary image: snapshot.images[0] (IMAGE) или cards[0] (DCO/DPA).
        # Prefer resized_image_url (обычно ~600px, 100-200KB, без watermark) —
        # original_image_url даёт full-res (1-2 MB), раздувает отчёт.
        # resized сохраняет качество для превью в отчёте.
        image_url = None
        images = s.get("images") or []
        if images:
            image_url = images[0].get("resized_image_url") or images[0].get("original_image_url")
        if not image_url:
            for card in (s.get("cards") or []):
                image_url = card.get("resized_image_url") or card.get("original_image_url")
                if image_url:
                    break

        # Video URL (HD приоритет). Заодно захватим video_preview_image_url как fallback image.
        video_url = None
        video_preview_image = None
        for v in (s.get("videos") or []):
            video_url = video_url or v.get("video_hd_url") or v.get("video_sd_url")
            video_preview_image = video_preview_image or v.get("video_preview_image_url")
            if video_url and video_preview_image:
                break
        if not video_url or not video_preview_image:
            for card in (s.get("cards") or []):
                video_url = video_url or card.get("video_hd_url") or card.get("video_sd_url")
                video_preview_image = video_preview_image or card.get("video_preview_image_url")
                if video_url and video_preview_image:
                    break

        # Если основной image_url пусто (VIDEO-only объявления) — используем preview кадр
        if not image_url and video_preview_image:
            image_url = video_preview_image

        # Body text — у DCO top-level это шаблон "{{product.brand}}".
        # Реальный рендеримый текст лежит в cards[i].body (может быть строкой или {text: ...}).
        def _is_tpl(t):
            return bool(t) and "{{" in t and "}}" in t

        body_text = ((s.get("body") or {}).get("text") or "").strip()
        if not body_text or _is_tpl(body_text):
            for card in (s.get("cards") or []):
                cb = card.get("body")
                if isinstance(cb, dict):
                    cb = cb.get("text")
                cb = (cb or "").strip()
                if cb and not _is_tpl(cb):
                    body_text = cb
                    break

        # Title — тоже может быть шаблоном у DCO, fallback на cards
        title = (s.get("title") or "").strip()
        if not title or _is_tpl(title):
            for card in (s.get("cards") or []):
                ct = (card.get("title") or "").strip()
                if ct and not _is_tpl(ct):
                    title = ct
                    break

        lib_id = str(raw.get("ad_archive_id") or "")
        if not lib_id:
            return None

        n_card_variants = len(s.get("cards") or [])

        return {
            "library_id":              lib_id,
            "page_name":               raw.get("page_name") or s.get("page_name") or "",
            "display_format":          s.get("display_format"),
            "is_active":               raw.get("is_active"),
            "start_date":              raw.get("start_date"),
            "end_date":                raw.get("end_date"),
            "platforms":               raw.get("publisher_platform") or [],
            "branded_content":         s.get("branded_content"),
            "title":                   title,
            "body_text":               body_text,
            "caption":                 s.get("caption") or "",
            "link_description":        s.get("link_description") or "",
            "link_url":                s.get("link_url") or "",
            "cta_text":                s.get("cta_text") or "",
            "cta_type":                s.get("cta_type") or "",
            "image_url":               image_url,
            "video_url":               video_url,
            "page_profile_uri":        s.get("page_profile_uri") or "",
            "page_profile_picture_url": s.get("page_profile_picture_url") or "",
            "page_like_count":         s.get("page_like_count"),
            "detail_url":              f"https://www.facebook.com/ads/library/?id={lib_id}",
            "n_card_variants":         n_card_variants,   # сколько carousel-вариантов
            # image_local заполнится позже в _download_ad_images
            "image_local":             None,
        }
    except Exception:
        return None


def _extract_ads_from_json(html: str, limit: int = 10,
                            target_page_id: str = None,
                            target_name: str = None) -> dict:
    """
    Вытаскивает структурированные ad records из Relay JSON payloads в HTML.
    FB Ads Library hydrate'ит страницу через GraphQL — данные доступны в
    <script type="application/json">...</script> блоках.

    Определяет mode по `ad_library_main.ad_library_page_info`:
      - non-null → advertiser-filtered (page mode) — результат авторитетный, без фильтра
      - null    → keyword search — шум возможен, нужен пост-фильтр

    Post-filter (в keyword mode):
      - target_page_id → точный match snapshot.page_id
      - target_name    → fuzzy match (difflib >= 0.75) snapshot.page_name
      - ни то ни то    → возвращаем как есть (unfiltered, 'keyword_raw')

    Returns dict:
      {
        ads: [...],          # top-N matched ads
        mode: 'page' | 'keyword_filtered_by_page_id' | 'keyword_filtered_by_name' | 'keyword_raw',
        raw_total: int,      # search_results_connection.count (total FB returned)
        matched_count: int,  # after post-filter (== raw_total for page mode)
      }
    """
    all_ads = []
    seen_ids = set()
    page_mode_signal = None  # True если встретили ad_library_page_info != null
    raw_total = 0

    for m in re.finditer(
        r'<script type="application/json"[^>]*>(.+?)</script>',
        html, flags=re.DOTALL
    ):
        payload = m.group(1)
        try:
            doc = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue

        # Mode signal: ad_library_page_info лежит рядом с ad_library_main (sibling),
        # а не внутри него. Ищем напрямую по дереву.
        if not page_mode_signal:
            for pi in _walk_for_key(doc, "ad_library_page_info"):
                if pi and isinstance(pi, dict) and pi.get("page_info"):
                    page_mode_signal = True
                    break

        # Ads из search_results_connection (независимо от mode)
        for conn in _walk_for_key(doc, "search_results_connection"):
            if not isinstance(conn, dict):
                continue
            c = conn.get("count")
            if isinstance(c, int) and c > raw_total:
                raw_total = c

            for edge in (conn.get("edges") or []):
                node = (edge or {}).get("node") or {}
                for raw_ad in (node.get("collated_results") or []):
                    ad = _normalize_ad_record(raw_ad)
                    if ad and ad["library_id"] not in seen_ids:
                        seen_ids.add(ad["library_id"])
                        # Временные поля для фильтрации — удалим перед возвратом
                        s = raw_ad.get("snapshot") or {}
                        ad["_snap_pid"] = str(s.get("page_id") or raw_ad.get("page_id") or "")
                        ad["_snap_pname"] = s.get("page_name") or ""
                        all_ads.append(ad)

    # Mode detection + post-filter
    # Inclusive: ad совпадает если ЛИБО page_id exact match ЛИБО fuzzy name match (или оба).
    # page-mode URL (view_all_page_id) не используется → page_mode_signal обычно False.
    if page_mode_signal is True:
        mode = "page"
        matched = all_ads
        matched_count = raw_total
    elif target_page_id or target_name:
        from difflib import SequenceMatcher
        def _norm(s): return (s or "").strip().lower()
        tgt_pid = str(target_page_id) if target_page_id else None
        tgt_name = _norm(target_name) if target_name else None

        matched = []
        for a in all_ads:
            pid_hit = bool(tgt_pid and a["_snap_pid"] == tgt_pid)
            name_hit = bool(tgt_name and
                            SequenceMatcher(None, _norm(a["_snap_pname"]), tgt_name).ratio() >= 0.75)
            if pid_hit or name_hit:
                matched.append(a)

        matched_count = len(matched)
        if tgt_pid and tgt_name:
            mode = "keyword_filtered_by_pid_or_name"
        elif tgt_pid:
            mode = "keyword_filtered_by_page_id"
        else:
            mode = "keyword_filtered_by_name"
    else:
        mode = "keyword_raw"
        matched = all_ads
        matched_count = len(matched)

    trimmed = matched[:limit]
    for a in trimmed:
        a.pop("_snap_pid", None)
        a.pop("_snap_pname", None)

    return {
        "ads": trimmed,
        "mode": mode,
        "raw_total": raw_total,
        "matched_count": matched_count,
    }


def _parse_ad_library_html(html: str, limit: int = 10,
                            target_page_id: str = None,
                            target_name: str = None) -> dict:
    """Парсит HTML страницы Ad Library. Возвращает count, structured_ads (post-filtered),
    тексты, partnership флаг, ads_library_mode, raw_keyword_total.

    Если передан target_page_id — фильтруем ads в keyword mode по нему.
    Иначе — если передан target_name — fuzzy-match фильтр по page_name.
    Если ни того ни другого — всё сырое.
    """
    result = {"count": None, "status": "could_not_parse", "ad_texts": [],
              "partnership_ads": False, "partnership_count": 0,
              "structured_ads": [], "ads_library_mode": "unknown",
              "raw_keyword_total": 0}

    # Count
    json_count = re.search(
        r'"search_results_connection"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)', html
    )
    if json_count:
        result["count"] = int(json_count.group(1))
        result["status"] = "active" if result["count"] > 0 else "no_active_ads"
        result["method"] = "json"
    else:
        heading = re.search(r'~?(\d[\d,\s]*)\s+results?', html, re.IGNORECASE)
        if heading:
            try:
                count = int(re.sub(r'[,\s]', '', heading.group(1)))
                if 0 < count < 1000000:
                    result["count"] = count
                    result["status"] = "active"
                    result["method"] = "heading"
            except ValueError:
                pass
        if result["count"] is None:
            if any(s in html.lower() for s in ['no ads match', 'no results', '"edges":[]', '"count":0']):
                result["count"] = 0
                result["status"] = "no_active_ads"
                result["method"] = "empty_signal"

    # ── NEW: structured ads из Relay JSON ───────────────────────────
    extracted = _extract_ads_from_json(
        html, limit=limit,
        target_page_id=target_page_id, target_name=target_name
    )
    structured_ads = extracted["ads"]
    result["structured_ads"] = structured_ads
    result["ads_library_mode"] = extracted["mode"]
    result["raw_keyword_total"] = extracted["raw_total"]

    # Главное поле count — теперь это matched_count (после пост-фильтра),
    # а raw_keyword_total хранит исходное число из FB на случай transparency
    if extracted["mode"] == "page" or structured_ads or extracted["matched_count"] == 0:
        result["count"] = extracted["matched_count"]
        result["status"] = "active" if extracted["matched_count"] > 0 else "no_active_ads"
        result["method"] = "json_" + extracted["mode"]

    if structured_ads:
        # Деривим плоский ad_texts список из отфильтрованных ads (backward compat)
        seen = set()
        unique_texts = []
        for ad in structured_ads:
            t = (ad.get("body_text") or "").strip()
            if t and t not in seen:
                seen.add(t)
                unique_texts.append(t)
        result["ad_texts"] = unique_texts
        result["partnership_count"] = sum(1 for a in structured_ads if a.get("branded_content"))
        result["partnership_ads"] = result["partnership_count"] > 0
        result["extraction_method"] = "json"
    elif extracted["raw_total"] == 0:
        # JSON нашёл 0 ads вообще — fallback на regex (redundant, но на всякий случай)
        texts = re.findall(r'white-space: pre-wrap[^>]*><span>([^<]{10,600})', html)
        seen = set()
        unique_texts = []
        for t in texts:
            t = t.strip()
            if t not in seen:
                seen.add(t)
                unique_texts.append(t)
        result["ad_texts"] = unique_texts
        partnership_count = len(re.findall(r'branded_content', html, re.IGNORECASE))
        estimated = max(0, partnership_count // 3)
        result["partnership_ads"] = estimated > 0
        result["partnership_count"] = estimated
        result["extraction_method"] = "regex_fallback"
    else:
        # JSON нашёл raw ads, но пост-фильтр отсёк все → честные нули
        # (не падаем в regex fallback — он считает шум от всех 109 advertiser'ов)
        result["ad_texts"] = []
        result["partnership_count"] = 0
        result["partnership_ads"] = False
        result["extraction_method"] = "json_all_filtered_out"

    return result


def _download_ad_images(domain: str, structured_ads: list,
                         status_label: str = "") -> list:
    """Скачивает главную картинку каждого ad record'а (по image_url из structured_ads).
    Имя файла: ad_{library_id}.jpg — для однозначной связи с текстом в fb.json.
    status_label: '' / 'active' / 'inactive' — подпапка для разделения 3-pass scan'a.
    Мутирует structured_ads: проставляет ad['image_local'] для HTML-репорта.
    Возвращает список путей скачанных файлов.

    NOTE: см. CLAUDE.md "Architectural backlog" — этот listing-side download
    запланирован к удалению; картинки должны приходить из detail modal scan
    (Step 5 fb_ad_modal_parse). Пока оставлено для совместимости.
    """
    import urllib.request
    from pathlib import Path

    img_dir = Path("scans") / domain / "fb_ads_images"
    if status_label:
        img_dir = img_dir / status_label
    img_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for ad in structured_ads:
        lib_id = ad.get("library_id")
        img_url = ad.get("image_url")
        if not (lib_id and img_url):
            continue
        try:
            ext = ".png" if ".png" in img_url.split("?")[0].lower() else ".jpg"
            filename = f"ad_{lib_id}{ext}"
            path = img_dir / filename

            req = urllib.request.Request(img_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()

            if len(data) < 5000:
                # Удаляем stale файл с прошлого rerun'а если он был — иначе
                # битый старый файл болтался бы рядом с обновлённым fb.json без
                # image_local (несоответствие). См. rerun edge case в comments.
                path.unlink(missing_ok=True)
                print(f"      ⚠️  Слишком маленький файл для ad {lib_id}, skip")
                continue

            path.write_bytes(data)
            # image_local — путь относительно scans/{domain}/, чтобы HTML репорт
            # мог легко подставить через relative src
            rel_dir = f"fb_ads_images/{status_label}/" if status_label else "fb_ads_images/"
            ad["image_local"] = f"{rel_dir}{filename}"
            saved.append(str(path))
            print(f"      📷 Сохранено: {filename}")
        except Exception as e:
            print(f"      ⚠️  Не удалось скачать ad {lib_id}: {str(e)[:60]}")

    return saved


def _build_ad_library_url(display_name: str, country: str, status: str) -> str:
    """status ∈ {'all','active','inactive'}"""
    keyword = display_name.strip().replace(" ", "%20")
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status={status}&ad_type=all&country={country}"
        f"&is_targeted_country=false&media_type=all"
        f"&q={keyword}"
        f"&search_type=keyword_unordered"
        f"&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )


def _scan_one_status(page, url: str, status_label: str, domain: str,
                      display_name: str, download_images: bool,
                      target_page_id: str = None,
                      target_name: str = None) -> dict:
    """Открывает URL, парсит HTML, скачивает картинки, сохраняет тексты.
    status_label: 'all' / 'active' / 'inactive'.

    target_page_id / target_name — фильтры для _parse_ad_library_html.
    Если переданы — keyword search post-filter активен (по page_id exact OR fuzzy name).
    Если None — keyword_raw mode (без фильтра, для backward-compat вызовов)."""
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
    parsed = _parse_ad_library_html(html,
                                     target_page_id=target_page_id,
                                     target_name=target_name)
    parsed["search_term"] = display_name
    parsed["status_filter"] = status_label

    # Скачиваем изображения только для active/inactive (не для all — он только для счётчика)
    # Источник URL'ов — structured_ads из _parse_ad_library_html, не DOM scrape.
    saved_images = []
    if download_images and domain and status_label in ("active", "inactive"):
        structured_ads = parsed.get("structured_ads") or []
        if parsed.get("count", 0) > 0 and structured_ads:
            print(f"      📷 Скачиваю изображения [{status_label}]...")
            # Мутирует structured_ads — проставляет image_local
            saved_images = _download_ad_images(domain, structured_ads,
                                                status_label=status_label)
    parsed["saved_images"] = saved_images

    # Сохраняем тексты с суффиксом
    if domain and parsed.get("ad_texts") and status_label in ("active", "inactive"):
        from pathlib import Path
        texts_dir = Path("scans") / domain / "fb_ads_images"
        texts_dir.mkdir(parents=True, exist_ok=True)
        texts_path = texts_dir / f"ad_texts_{status_label}.txt"
        with open(texts_path, "w", encoding="utf-8") as f:
            f.write(f"Ad texts for: {display_name} [{status_label}]\n")
            f.write(f"Total: {len(parsed['ad_texts'])}\n")
            f.write("=" * 60 + "\n\n")
            for i, text in enumerate(parsed["ad_texts"], 1):
                f.write(f"[{i}]\n{text}\n\n")
        print(f"      📄 Тексты [{status_label}]: {texts_path}")
        parsed["saved_texts_path"] = str(texts_path)

    return parsed


def get_ads_data(display_name: str, page_id: str = None,
                  country: str = "ALL", fb_page_url: str = None,
                  domain: str = "", download_images: bool = True) -> dict:
    """
    3-проходный скан Ad Library (только LISTING — без deep-scan модалок):
      1) active_status=all → есть ли что-то вообще
      2) если есть — active_status=active
      3) если total > active — active_status=inactive
    Возвращает {total_ever, active, inactive, и back-compat поля}.

    Filter activation: page_id и display_name пробрасываются в каждый
    _scan_one_status → _parse_ad_library_html → _extract_ads_from_json для
    post-filter (page_id exact OR fuzzy name >= 0.75). Если page_id None —
    останется только name-filter; если оба None — keyword_raw mode (шум).

    Deep-scan модалок (Reach/демография/disclaimer/advertiser/lead-form) живёт
    в отдельном модуле — см. fb_scan.py (orchestrator) и Modules 3/4/5.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"total_ever": None, "error": "playwright not installed"}

    result = {
        "total_ever": None,
        "active": None,
        "inactive": None,
        "search_term": display_name,
        "country": country,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()

            # ── Pass 1: total ever (active_status=all) ─────────────────
            print(f"      🔎 Проход 1/3: все объявления (active+inactive)...")
            url_all = _build_ad_library_url(display_name, country, "all")
            all_data = _scan_one_status(page, url_all, "all", domain,
                                         display_name, download_images=False,
                                         target_page_id=page_id,
                                         target_name=display_name)
            total = all_data.get("count")
            result["total_ever"] = total

            if not total or total == 0:
                print(f"      ❌ Объявлений не найдено вообще")
                browser.close()
                return result

            print(f"      📊 Всего объявлений (когда-либо): {total}")

            # ── Pass 2: active only ────────────────────────────────────
            print(f"      🔎 Проход 2/3: активные объявления...")
            url_active = _build_ad_library_url(display_name, country, "active")
            active_data = _scan_one_status(page, url_active, "active", domain,
                                            display_name, download_images,
                                            target_page_id=page_id,
                                            target_name=display_name)
            active_count = active_data.get("count", 0) or 0
            if active_count > 0:
                result["active"] = active_data
                print(f"      ✅ Активных: {active_count}")
            else:
                print(f"      ➖ Активных нет (но есть в архиве)")

            # ── Pass 3: inactive only (только если есть смысл) ────────
            if total > active_count:
                print(f"      🔎 Проход 3/3: неактивные объявления...")
                url_inactive = _build_ad_library_url(display_name, country, "inactive")
                inactive_data = _scan_one_status(page, url_inactive, "inactive", domain,
                                                  display_name, download_images,
                                                  target_page_id=page_id,
                                                  target_name=display_name)
                inactive_count = inactive_data.get("count", 0) or 0
                if inactive_count > 0:
                    result["inactive"] = inactive_data
                    print(f"      📦 Неактивных: {inactive_count}")
            else:
                print(f"      ➖ Все объявления активны — inactive скан не нужен")

            browser.close()

        # ── Back-compat: добавляем плоские поля чтобы старые консьюмеры работали ─
        active = result.get("active") or {}
        inactive = result.get("inactive") or {}

        # Тексты — объединение (active в приоритете)
        combined_texts = list(active.get("ad_texts") or [])
        for t in (inactive.get("ad_texts") or []):
            if t not in combined_texts:
                combined_texts.append(t)

        # Картинки — объединение (отдельные подпапки уже разделены)
        combined_images = list(active.get("saved_images") or []) + \
                          list(inactive.get("saved_images") or [])

        # Partnership — OR / sum
        partnership = bool(active.get("partnership_ads")) or bool(inactive.get("partnership_ads"))
        partnership_n = (active.get("partnership_count") or 0) + (inactive.get("partnership_count") or 0)

        # Combined structured_ads — active первыми, потом inactive (dedup by library_id).
        # Нужно для generate_site_report.py / generate_batch_report.py — они ждут
        # blissful-style flat schema на корне fb account record'a.
        combined_structured = list(active.get("structured_ads") or [])
        seen_ids = {a.get("library_id") for a in combined_structured if a.get("library_id")}
        for a in (inactive.get("structured_ads") or []):
            lib_id = a.get("library_id")
            if lib_id and lib_id not in seen_ids:
                combined_structured.append(a)
                seen_ids.add(lib_id)

        result.update({
            "count": active.get("count") or 0,            # back-compat: count = active count
            "ad_texts": combined_texts,
            "saved_images": combined_images,
            "partnership_ads": partnership,
            "partnership_count": partnership_n,
            # NEW flat fields (для generate_site_report / generate_batch_report):
            "structured_ads":     combined_structured,
            "ads_library_mode":   active.get("ads_library_mode") or inactive.get("ads_library_mode") or "unknown",
            "raw_keyword_total":  max(active.get("raw_keyword_total") or 0,
                                      inactive.get("raw_keyword_total") or 0),
            "extraction_method":  active.get("extraction_method") or inactive.get("extraction_method") or "unknown",
        })

        return result

    except Exception as e:
        return {"total_ever": None, "error": str(e)[:100],
                "search_term": display_name, "country": country}


# Back-compat alias — старые вызовы не сломаются
get_active_ads_count = get_ads_data

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

        # Fetch homepage: requests → Playwright fallback при 403/error.
        # Playwright обходит Cloudflare/WAF и отдаёт тот же HTML что пользователь
        # видит в браузере — вместе с FB-линкой в footer, даже если поверх висят
        # cookie/geo popups (они не скрывают static HTML).
        _html, _homepage_status, _fetch_method, _homepage_error = fetch_homepage(base_url)
        if _fetch_method == "playwright":
            print(f"    ✓ Homepage получен через Playwright (HTTP {_homepage_status})")

        # Для country detection нужны headers — они есть только в requests-ответе.
        # Если Playwright — оставляем пустыми, detect_site_country свалится на TLD.
        _headers = {}
        if _fetch_method == "requests":
            try:
                _r = requests.get(base_url, headers=HEADERS, timeout=10, allow_redirects=True)
                _headers = dict(_r.headers)
            except Exception:
                pass

        country_result = detect_site_country(urlparse(base_url).netloc, html=_html, response_headers=_headers)
        site_country = country_result["country"]
        site_country_source = country_result["source"]

        print(f"\n🌍 Страна сайта: {site_country}  (via {site_country_source})")
        print(f"\n🔍 Ищу Facebook аккаунты на {target}...")
        # Передаём уже скачанный HTML — избегаем повторного fetch
        handles = find_all_fb_handles(base_url, html=_html)

        # Homepage blocked flag — триггер brand-name fallback только если оба способа
        # (requests + Playwright) не смогли пробиться.
        homepage_blocked = _fetch_method == "blocked_by_waf"

        if not handles:
            full_domain = urlparse(base_url).netloc
            discovery_meta = {
                "homepage_status":        _homepage_status,
                "homepage_error":         _homepage_error,
                "homepage_fetch_method":  _fetch_method,
                "fallback_attempted":     False,
                "fallback_keyword":       None,
                "fallback_result":        None,
            }

            if homepage_blocked:
                print(f"  ⚠️  Homepage заблокирован (status={_homepage_status}, err={_homepage_error})")
                print(f"  🔄 Пробую brand-name fallback — поиск в Ads Library по '{brand_name}'...")
                discovery_meta["fallback_attempted"] = True
                discovery_meta["fallback_keyword"] = brand_name

                ads_urls = build_ads_library_urls(brand_name)
                # 3-pass scan по brand-keyword (без page_id — пост-фильтр по name)
                ads_data = get_ads_data(brand_name, country="ALL", domain=full_domain)
                fb_total = ads_data.get("total_ever") or 0

                if fb_total > 0:
                    active_block = ads_data.get("active") or {}
                    inactive_block = ads_data.get("inactive")
                    active_count = active_block.get("count", 0) or 0
                    print(f"  ✅ Brand-fallback нашёл {fb_total} объявлений (всего; активных {active_count})")
                    discovery_meta["fallback_result"] = f"found_{fb_total}_ads"
                    virtual_account = {
                        "handle":             None,
                        "display_name":       brand_name,
                        "url":                None,
                        "page_id":            None,
                        "alive":              True,
                        # 3-pass структура
                        "total_ever":         fb_total,
                        "active":             active_block,
                        "inactive":           inactive_block,
                        # back-compat плоские поля
                        "active_ads_count":   active_count,
                        "partnership_ads":    ads_data.get("partnership_ads", False),
                        "partnership_count":  ads_data.get("partnership_count", 0),
                        "ad_texts":           ads_data.get("ad_texts", []),
                        "saved_images":       ads_data.get("saved_images", []),
                        "structured_ads":     ads_data.get("structured_ads", []),
                        "ads_library_mode":   ads_data.get("ads_library_mode", "unknown"),
                        "raw_keyword_total":  ads_data.get("raw_keyword_total", 0),
                        "extraction_method":  ads_data.get("extraction_method", "unknown"),
                        "ads_search_term":    brand_name,
                        "ads_library":        ads_urls,
                        "discovery_method":   "brand_keyword_fallback",
                        "confidence_note":    "Homepage was blocked. These ads were found by "
                                              "searching Ads Library with brand-name keyword and "
                                              "then fuzzy-matching advertiser names. "
                                              "Manual verification recommended.",
                    }
                    output = {
                        "target":              target,
                        "brand_name":          brand_name,
                        "site_country":        site_country,
                        "site_country_source": site_country_source,
                        "accounts":            [virtual_account],
                        "discovery_meta":      discovery_meta,
                    }
                    filename = scan_path(full_domain, "fb.json")
                    with open(filename, "w", encoding="utf-8") as f:
                        json.dump(output, f, indent=2, ensure_ascii=False)
                    print(f"\n💾 Сохранено (via brand-fallback): {filename}")
                    return output
                else:
                    print(f"  ❌ Brand-fallback: 0 ads по '{brand_name}'")
                    discovery_meta["fallback_result"] = "no_ads_found"
            else:
                print(f"  ❌ Facebook ссылки не найдены (homepage статус {_homepage_status})")

            # Save empty fb.json with discovery_meta so report can explain what happened
            output = {
                "target":              target,
                "brand_name":          brand_name,
                "site_country":        site_country,
                "site_country_source": site_country_source,
                "accounts":            [],
                "discovery_meta":      discovery_meta,
            }
            filename = scan_path(full_domain, "fb.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            print(f"\n💾 Сохранено (empty): {filename}")
            return output

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
        fb_page_id = status.get("page_id") or item.get("page_id")

        print(f"    ✓ Published | Display name: {display_name}"
              + (f" | Page ID: {fb_page_id}" if fb_page_id else ""))

        ads_urls = build_ads_library_urls(display_name, page_id=fb_page_id)
        print(f"    📢 Ads Library: {ads_urls['ALL']['active_only']}")

        print(f"    🔢 Проверяю рекламу по имени '{display_name}'...")
        full_domain = urlparse(base_url).netloc if not is_handle else brand_name
        ads_data = get_ads_data(display_name, page_id=fb_page_id, country="ALL",
                                 fb_page_url=fb_page_url, domain=full_domain)

        total_ever     = ads_data.get("total_ever")
        active_block   = ads_data.get("active")    # dict | None
        inactive_block = ads_data.get("inactive")  # dict | None
        # back-compat плоские поля (для step1_sitemap.py и старых консьюмеров)
        count          = ads_data.get("count", 0)
        ad_texts       = ads_data.get("ad_texts", [])
        partnership    = ads_data.get("partnership_ads", False)
        partnership_n  = ads_data.get("partnership_count", 0)
        saved_images   = ads_data.get("saved_images", [])

        # ── Печать итогов по 3-х проходному скану ──────────────────────────
        if total_ever is None:
            print(f"    ⚠️  Не удалось определить количество")
        elif total_ever == 0:
            print(f"    ❌ Объявлений нет (никогда не крутились)")
        else:
            active_n   = (active_block or {}).get("count", 0) or 0
            inactive_n = (inactive_block or {}).get("count", 0) or 0
            print(f"    📊 Всего: {total_ever}  |  ✅ активных: {active_n}  |  📦 архив: {inactive_n}")

        # Mode/raw transparency — показывает как фильтровались keyword результаты
        ads_lib_mode = ads_data.get("ads_library_mode")
        raw_total = ads_data.get("raw_keyword_total")
        if raw_total and raw_total != count:
            mode_label = {
                "page":                              "verified by page_id",
                "keyword_filtered_by_pid_or_name":   "keyword + page_id+name filter",
                "keyword_filtered_by_page_id":       "keyword + page_id filter",
                "keyword_filtered_by_name":          "keyword + fuzzy name filter",
                "keyword_raw":                       "keyword raw (unfiltered)",
                "unknown":                           "unknown",
            }.get(ads_lib_mode, ads_lib_mode)
            print(f"    🔍 Filter mode: {mode_label} ({count} matched / {raw_total} raw)")

        if partnership:
            print(f"    🤝 Partnership ads: да (~{partnership_n})")
        if ad_texts:
            print(f"    📝 Текстов объявлений (active+archive): {len(ad_texts)}")
        if saved_images:
            print(f"    🖼  Изображений скачано: {len(saved_images)}")

        results.append({
            "handle":             handle,
            "display_name":       display_name,
            "url":                fb_page_url,
            "page_id":            fb_page_id,
            "alive":              True,
            # 3-проходная структура
            "total_ever":         total_ever,
            "active":             active_block,
            "inactive":           inactive_block,
            # back-compat плоские поля + filter transparency
            "active_ads_count":   count,
            "partnership_ads":    partnership,
            "partnership_count":  partnership_n,
            "ad_texts":           ad_texts,
            "saved_images":       saved_images,
            "structured_ads":     ads_data.get("structured_ads", []),
            "ads_library_mode":   ads_data.get("ads_library_mode", "unknown"),
            "raw_keyword_total":  ads_data.get("raw_keyword_total", 0),
            "extraction_method":  ads_data.get("extraction_method", "unknown"),
            "ads_search_term":    display_name,
            "ads_library":        ads_urls,
            "discovery_method":   "homepage_link",
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

    filename = scan_path(urlparse(base_url).netloc if not is_handle else brand_name, "fb.json")
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
