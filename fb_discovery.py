"""
TNC — Facebook Discovery
========================
Homepage → FB handles → classification → Page ID pipeline.

Отвечает за discovery brand'a в Facebook'е:
- detect_site_country: страна сайта (header → lang-attr → TLD)
- fetch_homepage: HTTP fetch с Playwright fallback при WAF
- find_all_fb_handles: 4 формата FB URL в HTML + классификация id_type
  (page / profile / unknown — определяет можно ли доверять числовому ID в URL)
- prioritize_handles: сортировка handle'ов (id_type=page приоритетнее всего)
- check_fb_page_alive_playwright: alive check + display_name (через Playwright)
- find_delegate_page_id: extraction Page ID из profile/vanity через delegate_page
  в GraphQL response (мост profile → Page, для случаев когда на сайте линкуется
  личный профиль владельца, а реклама крутится с отдельной Page)

Используется: fb_page_id.py (orchestrator), fb_page_finder.py.
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

SKIP_FB_PATHS = {
    "sharer", "share", "tr", "dialog", "plugins", "photo", "video",
    "events", "groups", "pages", "help", "privacy", "legal", "ads",
    "business", "policies", "about", "login", "watch", "marketplace",
    "gaming", "fundraisers", "messenger",
    # Служебные path'ы для других форматов (parsятся отдельно по своим regex)
    "profile.php", "profile", "people", "p",
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

    Каждый найденный handle получает поле id_type:
      - "page"    — числовой ID в URL гарантированно Page ID (формат /pages/)
      - "profile" — числовой ID в URL это profile ID (НЕ Page, рекламы быть не может)
      - "unknown" — числового ID нет, тип определим только открыв страницу (vanity)

    Для id_type="page" поле page_id содержит готовый Page ID.
    Для id_type="profile" поле page_id=None, числовой ID лежит в profile_id.
    Для id_type="unknown" оба поля None.
    """
    found = {}  # key → {handle, url, page_id, profile_id, id_type, format, ...}

    if html is None:
        try:
            r = requests.get(base_url, headers=HEADERS, timeout=10)
            html = r.text
        except Exception as e:
            print(f"  ⚠️  Ошибка при загрузке сайта: {e}")
            return []

    # Формат 1: facebook.com/vanityname  →  id_type=unknown (числа в URL нет)
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
                "profile_id": None,
                "id_type": "unknown",
                "format": "vanity",
            }

    # Формат 2: facebook.com/people/Name/ID/  →  id_type=profile (это НЕ Page)
    for name, profile_id in re.findall(
        r'facebook\.com/people/([^/"\']+)/(\d{10,})/?',
        html
    ):
        key = f"people:{profile_id}"
        if key not in found:
            found[key] = {
                "handle": name.lower().replace("-", "_"),
                "url": f"https://www.facebook.com/people/{name}/{profile_id}/",
                "page_id": None,
                "profile_id": profile_id,
                "id_type": "profile",
                "format": "people",
                "display_name": name.replace("-", " "),
            }

    # Формат 3: facebook.com/profile.php?id=ID  →  id_type=profile (это НЕ Page)
    for profile_id in re.findall(
        r'facebook\.com/profile\.php\?id=(\d{10,})',
        html
    ):
        key = f"profile:{profile_id}"
        if key not in found:
            found[key] = {
                "handle": f"profile_{profile_id}",
                "url": f"https://www.facebook.com/profile.php?id={profile_id}",
                "page_id": None,
                "profile_id": profile_id,
                "id_type": "profile",
                "format": "profile",
            }

    # Формат 4: facebook.com/pages/Name/ID  →  id_type=page (это РЕАЛЬНО Page ID)
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
                "profile_id": None,
                "id_type": "page",
                "format": "pages",
            }

    return list(found.values())



def prioritize_handles(handles: list, brand_name: str) -> list:
    """
    Сортирует handles по составному ключу (id_type tier, brand match tier).

    Первый приоритет — id_type:
      0. page    — числовой ID гарантированно Page ID (готовый, в Ads Library сразу)
      1. unknown — vanity, числа в URL нет (может быть Page или profile — открываем)
      2. profile — числовой ID это profile (точно НЕ Page, рекламы быть не может,
                   но потенциально через delegate_page.id можно найти связанный Page)

    Второй приоритет — match с brand_name (как раньше):
      0. Точное совпадение handle с brand
      1. Содержит brand, нет региональных суффиксов
      2. Содержит brand + регион (canada, uk, ...)
      3. Всё остальное
    """
    brand = brand_name.lower().replace("-", "").replace(".", "")
    id_type_tier = {"page": 0, "unknown": 1, "profile": 2}

    def score(item):
        h = item["handle"].lower().replace("-", "").replace(".", "")
        # Brand match score (как было)
        if h == brand:
            brand_score = 0
        elif brand in h:
            has_suffix = any(h.endswith(s) or h.startswith(s) for s in COUNTRY_SUFFIXES)
            brand_score = 1 if not has_suffix else 2
        else:
            brand_score = 3

        # id_type получает абсолютный приоритет — сначала сортируем по нему,
        # внутри tier'а — по brand match
        return (id_type_tier.get(item.get("id_type", "unknown"), 1), brand_score)

    return sorted(handles, key=score)


# ─── Проверка живой/битой ссылки + display_name ─────────────────────────────

def check_fb_page_alive_playwright(handle: str) -> dict:
    """
    Открывает страницу facebook.com/{handle} в headless Chromium и за один проход:
      - alive: жива ли страница (нет ли dead signals в HTML)
      - display_name: имя страницы из <title> / <h1>

    Используется и для готового Page ID (Ветка A: /pages/Name/{ID}/), и
    для verification после delegate_page extraction (Ветка B step 2).

    Returns: {"alive": bool, "reason": str, "display_name": str | None}
    """
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
            browser.close()

            # Display name из <title> / <h1>
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

            # Alive check — dead signals в HTML
            dead_signals = [
                "this page isn't available",
                "the link you followed may be broken",
                "this page has been removed",
                "sorry, this page isn't available",
            ]
            for signal in dead_signals:
                if signal in html:
                    return {"alive": False, "reason": f"playwright: {signal[:40]}",
                            "display_name": display_name}

            return {"alive": True, "reason": "playwright_ok", "display_name": display_name}
    except Exception:
        return {"alive": True, "reason": "playwright_failed_assuming_alive",
                "display_name": None}


# ─── Поиск Page ID через delegate_page (мост profile → Page) ────────────────

def find_delegate_page_id(fb_url: str) -> str | None:
    """
    Открывает FB-страницу (profile/vanity) и ищет связанный Page ID через
    'delegate_page' паттерн в GraphQL response body.

    Зачем: личный профиль владельца бизнеса может быть админом отдельной Page
    (где крутится реклама). FB GraphQL preloader response возвращает связь
    profile → delegate_page → id.

    Пример (redacted-prospect.example):
      На сайте ссылка на /profile.php?id=61500000000000 (личный профиль).
      Реклама крутится с Page 1234567890123456.
      delegate_page.id в GraphQL response даёт мост между ними.

    Returns: Page ID (string) или None если delegate_page не найден.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    delegate_ids = []

    # GraphQL response'ы FB содержат `delegate_page` объект для profile'ов
    # которые являются админами Page. Несколько вариантов кодировки —
    # пробуем все основные.
    delegate_patterns = [
        r'"delegate_page"\s*:\s*\{\s*"id"\s*:\s*"(\d{10,})"',
        r'"delegate_page"\s*:\s*\{[^{}]*?"id"\s*:\s*"(\d{10,})"',
        r'"delegate_page_id"\s*:\s*"(\d{10,})"',
        r'"delegate_page\.id"\s*:\s*"(\d{10,})"',
    ]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()

            def on_response(response):
                try:
                    if "facebook.com" not in response.url:
                        return
                    if response.status != 200:
                        return
                    ct = response.headers.get("content-type", "")
                    if not any(t in ct for t in ["json", "javascript", "html"]):
                        return
                    body = response.body().decode("utf-8", errors="ignore")
                    for pattern in delegate_patterns:
                        for m in re.findall(pattern, body):
                            if len(m) >= 10:
                                delegate_ids.append(m)
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                page.goto(fb_url, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(2000)
            except Exception:
                try:
                    page.goto(fb_url, wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(3000)
                except Exception:
                    pass

            # Финальный HTML — иногда delegate_page лежит в inline scripts
            try:
                html = page.content()
                for pattern in delegate_patterns:
                    for m in re.findall(pattern, html):
                        if len(m) >= 10:
                            delegate_ids.append(m)
            except Exception:
                pass

            browser.close()
    except Exception as e:
        print(f"    ⚠️  delegate_page extraction failed: {str(e)[:80]}")
        return None

    if not delegate_ids:
        return None

    from collections import Counter
    counts = Counter(delegate_ids)
    best_id, hits = counts.most_common(1)[0]
    print(f"    🔗 delegate_page.id найден: {best_id} (встретился {hits} раз)")
    return best_id
