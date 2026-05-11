"""
TNC — Facebook Discovery
========================
Homepage → FB handle → page-alive → page_id pipeline.

Отвечает за discovery brand'a в Facebook'е:
- detect_site_country: страна сайта (header → lang-attr → TLD)
- fetch_homepage: HTTP fetch с Playwright fallback при WAF
- find_all_fb_handles: 5 форматов FB URL в HTML
- prioritize_handles: сортировка handle'ов по приоритету
- check_fb_page_alive / check_fb_page_alive_playwright: проверка живости + display_name + page_id
- get_page_id_requests / get_page_id_playwright / get_page_id: разные методы получения числового ID

Используется: fb_page_id.py (orchestrator), fb_page_finder.py, debug_fb_id.py.
"""

import re
import requests

from utils import HEADERS, setup_console
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
