"""
TNC Pipeline — Step 1: Sitemap Fetch & Classify
================================================
Получает все страницы сайта, классифицирует по URL.
Для SPA/маркетплейсов — находит паттерны страниц через Playwright.

Запуск:
    python step1_sitemap.py bandago.com
    python step1_sitemap.py hipcamp.com
    python step1_sitemap.py example.com --all
"""

import sys
import json
import re
import argparse
import time
from urllib.parse import urlparse, urljoin

try:
    import requests
    from xml.etree import ElementTree as ET
except ImportError:
    # utils недоступен без requests (тянет его при импорте) — минимальный cp1252-фикс инлайном
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print("❌ Установи: pip install requests")  # log не импортирован если requests упал — оставляем print
    sys.exit(1)

from page_classifier import classify_url, get_page_priority_label, save_pattern, load_patterns, ANTHROPIC_API_KEY
from platform_detector import detect_platform, print_platform_result, classify_shopify_page
from utils import get_scan_dir, scan_path, TeeLogger, setup_logging, HEADERS, safe_get, normalize_url, detect_site_language
from log import log_info, log_warn, log_error, log_debug, log_success, log_step, log_header, log_fire
from language_detector import discover_language_versions, keep_lang_subtree


DEFAULT_LIMIT = 65
MIN_EN_POOL = 5  # меньше этого EN-URL в пуле после языкового switch → докраулить EN-главную
# Pattern discovery (браузерный crawl) имеет смысл только когда sitemap тонкий.
# Если URL в sitemap уже >= этого порога — coverage хорошая, discovery не нужен
# (иначе зря ползаем браузером по огромному сайту типа Nissan, 9284 URL).
DISCOVERY_MAX_URLS = 50


# ─── Sitemap helpers ──────────────────────────────────────────────────────────

def looks_like_sitemap_content(text: str) -> bool:
    return "<loc>" in text or "sitemapindex" in text.lower() or "urlset" in text.lower()


def fetch_urls_from_xml(xml_text: str) -> list:
    log_debug(f"fetch_urls_from_xml: start len={len(xml_text)}")
    urls = []
    try:
        root = ET.fromstring(xml_text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            u = loc.text.strip() if loc.text else ""
            if u:
                urls.append(u)
        log_debug(f"fetch_urls_from_xml: ET-парсинг ок, найдено {len(urls)} loc")
    except ET.ParseError as e:
        log_debug(f"fetch_urls_from_xml: ET.ParseError ({e}) — fallback на regex")
        urls = re.findall(r'<loc>\s*(https?://[^\s<]+)\s*</loc>', xml_text)
        log_debug(f"fetch_urls_from_xml: regex fallback нашёл {len(urls)} loc")
    return urls


def expand_sitemap_index(urls: list) -> list:
    log_debug(f"expand_sitemap_index: start, {len(urls)} входных URL")
    expanded = []
    for u in urls:
        if re.search(r'sitemap', u, re.IGNORECASE) or u.endswith(".xml"):
            log_debug(f"expand_sitemap_index: похоже на вложенный sitemap → {u}")
            try:
                r = requests.get(u, headers=HEADERS, timeout=10)
                if r.status_code == 200 and looks_like_sitemap_content(r.text):
                    sub = fetch_urls_from_xml(r.text)
                    log_debug(f"expand_sitemap_index: {u} раскрыт в {len(sub)} под-URL")
                    for su in sub:
                        if re.search(r'sitemap', su, re.IGNORECASE) or su.endswith(".xml"):
                            log_debug(f"expand_sitemap_index: второй уровень sitemap → {su}")
                            try:
                                r2 = requests.get(su, headers=HEADERS, timeout=10)
                                if r2.status_code == 200:
                                    expanded.extend(fetch_urls_from_xml(r2.text))
                            except Exception as e:
                                log_debug(f"expand_sitemap_index: fetch второго уровня {su} упал: {e}")
                        else:
                            expanded.append(su)
                    continue
                else:
                    log_debug(f"expand_sitemap_index: {u} статус={r.status_code} или не sitemap-контент")
            except Exception as e:
                log_debug(f"expand_sitemap_index: fetch {u} упал: {e}")
        expanded.append(u)
    log_debug(f"expand_sitemap_index: итого {len(expanded)} URL")
    return expanded


def try_fetch_sitemap(url: str) -> list:
    log_debug(f"try_fetch_sitemap: пробую {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            log_debug(f"try_fetch_sitemap: {url} статус={r.status_code} — пусто")
            return []
        if not looks_like_sitemap_content(r.text):
            log_debug(f"try_fetch_sitemap: {url} не похоже на sitemap-контент — пусто")
            return []
        urls = fetch_urls_from_xml(r.text)
        urls = expand_sitemap_index(urls)
        log_debug(f"try_fetch_sitemap: {url} дал {len(urls)} URL")
        return urls
    except Exception as e:
        log_debug(f"try_fetch_sitemap: {url} упал: {e}")
        return []


def crawl_homepage_links(base_url: str) -> list:
    log_debug(f"crawl_homepage_links: start url={base_url}")
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=10)
        domain = urlparse(base_url).netloc
        hrefs = re.findall(r'href=["\']([^"\'#]+)["\']', r.text)
        log_debug(f"crawl_homepage_links: найдено {len(hrefs)} href на главной")
        urls = set()
        for h in hrefs:
            h = h.split("?")[0].split("#")[0]
            if h.startswith("/") and len(h) > 1:
                urls.add(urljoin(base_url, h))
            elif domain in h:
                urls.add(h)
        log_debug(f"crawl_homepage_links: отфильтровано {len(urls)} внутренних URL")
        return list(urls)
    except Exception as e:
        log_debug(f"crawl_homepage_links: {base_url} упал: {e}")
        return []


def _urls_belong_to_domain(urls: list, domain: str) -> bool:
    """Проверяет что хотя бы один URL принадлежит нашему домену."""
    if not urls:
        return False
    target = domain.lower()
    return any(target in u.lower() for u in urls[:50])


def _host_variants(base_url: str) -> list:
    """base_url + его www/no-www сиблинг — robots.txt может отличаться между ними.
    # Tested: 2026-07-07 on tinytronics.nl — no-www robots без Sitemap:, www robots с Sitemap:
    """
    p = urlparse(base_url)
    sibling = p.netloc[4:] if p.netloc.startswith("www.") else "www." + p.netloc
    return [f"{p.scheme}://{p.netloc}", f"{p.scheme}://{sibling}"]


def _looks_like_waf_challenge(text: str) -> bool:
    """Только маркеры, встречающиеся исключительно на Cloudflare-интерстишле —
    НЕ 'challenge-platform' (есть на обычных CF-страницах и в легитимных robots.txt)."""
    t = text.lower()
    return ("just a moment" in t or "_cf_chl_opt" in t
            or "enable javascript and cookies" in t)


def fetch_links_from_html_sitemap(url: str, site_domain: str) -> list:
    """
    Нестандартная HTML-карта (напр. OpenCart index.php?route=information/sitemap):
    не XML, но легитимный список ссылок. Тянем <a href> того же домена.
    Предохранитель: если редирект увёл со страницы карты (в финальном URL нет
    'sitemap') — считаем что карты нет, чтобы не скрейпить заглушку/главную.
    # Tested: 2026-07-07 on tinytronics.nl — 481 ссылок с HTML-карты, 468 после clean_urls
    #         (было: homepage crawl, 135). Регресс: aiby.com (level 1) и platinumlist.net
    #         (robots→XML) — источник и поведение не изменились.
    """
    log_debug(f"fetch_links_from_html_sitemap: пробую {url}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
        if r.status_code != 200 or _looks_like_waf_challenge(r.text):
            log_debug(f"fetch_links_from_html_sitemap: {url} статус={r.status_code}/waf — пусто")
            return []
        if "sitemap" not in r.url.lower():
            log_debug(f"fetch_links_from_html_sitemap: редирект увёл на {r.url} (не карта) — пусто")
            return []
        hrefs = re.findall(r'href=["\']([^"\'#]+)["\']', r.text)
        urls = set()
        for h in hrefs:
            if h.startswith("/") and len(h) > 1:
                urls.add(urljoin(r.url, h))
            elif site_domain in h:
                urls.add(h)
        log_debug(f"fetch_links_from_html_sitemap: {url} → {len(urls)} внутренних ссылок")
        return list(urls)
    except Exception as e:
        log_debug(f"fetch_links_from_html_sitemap: {url} упал: {e}")
        return []


def get_sitemap_urls(base_url: str) -> tuple:
    """
    Умный поиск sitemap — 4 уровня:
    1. Стандартные пути
    2. robots.txt с обоих хостов (www/no-www); XML или HTML-карта нашего домена
    3. Ссылки с 'sitemap' на главной
    4. Fallback — href с главной
    """
    site_domain = urlparse(base_url).netloc
    log_debug(f"get_sitemap_urls: start base_url={base_url} domain={site_domain}")

    standard_paths = [
        "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
        "/sitemap.xml.gz", "/wp-sitemap.xml", "/sitemap/", "/sitemap",
        "/sitemaps/sitemap.xml", "/news-sitemap.xml", "/pages-sitemap.xml",
        "/post-sitemap.xml",
    ]

    log_debug("get_sitemap_urls: уровень 1 — стандартные пути")
    for path in standard_paths:
        urls = try_fetch_sitemap(base_url + path)
        if urls and _urls_belong_to_domain(urls, site_domain):
            log_debug(f"get_sitemap_urls: найдено через стандартный путь {path}")
            return urls, base_url + path

    log_debug("get_sitemap_urls: уровень 2 — robots.txt")
    # robots.txt может отличаться на www и no-www (tinytronics: Sitemap: только на www).
    # Сначала собираем все Sitemap:-ссылки с обоих хостов (с дедупом), потом пробуем каждую один раз.
    sm_urls = []
    for robots_base in _host_variants(base_url):
        try:
            r = requests.get(f"{robots_base}/robots.txt", headers=HEADERS, timeout=10)
            if r.status_code != 200:
                log_debug(f"get_sitemap_urls: robots.txt {robots_base} статус={r.status_code}")
                continue
            if _looks_like_waf_challenge(r.text):
                log_warn(f"robots.txt {robots_base} — WAF-заглушка, пропускаю")
                continue
            found = re.findall(r'(?i)sitemap\s*:\s*(https?://\S+)', r.text)
            log_debug(f"get_sitemap_urls: robots.txt {robots_base} дал {len(found)} sitemap-ссылок")
            for sm_url in found:
                if sm_url not in sm_urls:
                    sm_urls.append(sm_url)
        except Exception as e:
            log_debug(f"get_sitemap_urls: уровень 2 (robots.txt {robots_base}) упал: {e}")

    for sm_url in sm_urls:
        urls = try_fetch_sitemap(sm_url)
        if urls and _urls_belong_to_domain(urls, site_domain):
            log_debug(f"get_sitemap_urls: найдено через robots.txt → {sm_url}")
            return urls, f"robots.txt → {sm_url}"
        elif urls:
            log_warn(f"robots.txt sitemap чужого домена ({sm_url}) — пропускаю")
            continue
        # XML не вышел. Если ссылка обещала XML (*.xml/.gz) — она мертва;
        # HTML-скрейп такой страницы дал бы мусор с 200-заглушки.
        if urlparse(sm_url).path.lower().endswith((".xml", ".xml.gz")):
            continue
        # Нестандартная карта (OpenCart index.php?route=information/sitemap и т.п.) — пробуем как HTML.
        urls = fetch_links_from_html_sitemap(sm_url, site_domain)
        if urls and _urls_belong_to_domain(urls, site_domain):
            log_debug(f"get_sitemap_urls: HTML-карта через robots.txt → {sm_url}")
            # 'fallback' в имени источника — run() оставляет Pattern Discovery включённым
            return urls, f"robots.txt html-fallback → {sm_url}"

    log_debug("get_sitemap_urls: уровень 3 — ссылки с 'sitemap' на главной")
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=10)
        all_hrefs = re.findall(r'(?:href|src)=["\']([^"\']+)["\']', r.text)
        sitemap_strings = re.findall(
            r'["\']([^"\']{0,100}sitemap[^"\']{0,100})["\']', r.text, re.IGNORECASE
        )
        candidates = set()
        for link in all_hrefs + sitemap_strings:
            if not re.search(r'sitemap', link, re.IGNORECASE):
                continue
            if link.startswith("http"):
                candidates.add(link.split("?")[0])
            elif link.startswith("/"):
                candidates.add(base_url + link.split("?")[0])
        log_debug(f"get_sitemap_urls: уровень 3 нашёл {len(candidates)} кандидатов")
        for candidate in sorted(candidates):
            urls = try_fetch_sitemap(candidate)
            if urls:
                log_debug(f"get_sitemap_urls: найдено через discovery → {candidate}")
                return urls, f"discovered → {candidate}"
    except Exception as e:
        log_debug(f"get_sitemap_urls: уровень 3 (discovery) упал: {e}")

    log_debug("get_sitemap_urls: уровень 4 — fallback homepage crawl")
    urls = crawl_homepage_links(base_url)
    return urls, "homepage crawl (fallback)"


# ─── Pattern Discovery через Playwright ──────────────────────────────────────

def discover_url_patterns(base_url: str, domain: str, known_urls: list) -> list:
    """
    Открывает сайт браузером, находит структурные паттерны URL
    которых нет в sitemap И не покрыты классификатором.
    Возвращает по одному примеру каждого нового паттерна.
    """
    log_debug(f"discover_url_patterns: start base_url={base_url} domain={domain} known={len(known_urls)}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        log_debug(f"discover_url_patterns: playwright не установлен ({e}) — пропускаю discovery")
        return []

    # Паттерны уже известные из sitemap
    known_patterns = set()
    for u in known_urls:
        path = urlparse(u).path
        parts = [p for p in path.split("/") if p]
        if len(parts) >= 2:
            known_patterns.add("/" + "/".join(parts[:2]))
        elif len(parts) == 1:
            known_patterns.add("/" + parts[0])
    log_debug(f"discover_url_patterns: {len(known_patterns)} известных паттернов из sitemap")

    discovered = []
    seen_patterns = set()  # паттерны которые уже добавили

    log_step("Pattern discovery: открываю браузер...", emoji="🔍")

    def process_link(link: str) -> bool:
        """Обрабатывает одну ссылку. Возвращает True если добавлен новый паттерн."""
        if domain not in link:
            return False
        link = link.split("?")[0].split("#")[0].rstrip("/")
        if not link:
            return False

        path = urlparse(link).path
        parts = [p for p in path.split("/") if p]

        if len(parts) >= 2:
            pattern = "/" + "/".join(parts[:2])
        elif len(parts) == 1:
            pattern = "/" + parts[0]
        else:
            return False

        # Уже знаем этот паттерн
        if pattern in known_patterns or pattern in seen_patterns:
            log_fire(f"process_link: паттерн {pattern} уже известен — пропуск")
            return False

        # Проверяем структурный паттерн через classifier
        # Используем корневой URL паттерна, не конкретный продукт
        pattern_url = f"https://{domain}{pattern}"
        test_result = classify_url(pattern_url)
        if test_result["type"] != "general":
            log_fire(f"process_link: паттерн {pattern} распознан классификатором как {test_result['type']} — не новый")
            seen_patterns.add(pattern)
            return False

        # Новый паттерн которого классификатор не знает
        log_fire(f"process_link: НОВЫЙ паттерн {pattern} ← {link}")
        seen_patterns.add(pattern)
        discovered.append({
            "url": link,
            "pattern": pattern,
            "source": "discovery",
        })
        return True

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
            ignore_https_errors=True,   # клиентский сайт: битый сертификат — не повод падать
        )
        page = context.new_page()

        pages_to_explore = [base_url]
        for u in known_urls[:10]:
            if classify_url(u)["priority"] <= 2:
                pages_to_explore.append(u)

        explored = set()

        log_debug(f"discover_url_patterns: исследую до 5 страниц из {len(pages_to_explore)} кандидатов")
        for explore_url in pages_to_explore[:5]:
            if explore_url in explored:
                continue
            explored.add(explore_url)
            log_debug(f"discover_url_patterns: открываю {explore_url}")

            try:
                page.goto(explore_url, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(2000)
            except Exception as e:
                log_debug(f"discover_url_patterns: networkidle goto {explore_url} упал ({e}) — пробую domcontentloaded")
                try:
                    page.goto(explore_url, wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(1500)
                except Exception as e2:
                    log_debug(f"discover_url_patterns: domcontentloaded goto {explore_url} тоже упал ({e2}) — пропуск")
                    continue

            # Собираем ссылки + скролл
            all_links = set()
            for scroll_pos in [0, 0.5, 1.0]:
                try:
                    page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {scroll_pos})")
                    page.wait_for_timeout(800)
                    links = page.evaluate("""
                        () => Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => h.startsWith('http'))
                            .slice(0, 300)
                    """)
                    all_links.update(links)
                except Exception as e:
                    log_debug(f"discover_url_patterns: сбор ссылок на scroll={scroll_pos} упал: {e}")

            log_debug(f"discover_url_patterns: {explore_url} → {len(all_links)} ссылок собрано")
            for link in all_links:
                if process_link(link):
                    log_success(f"Новый паттерн: {discovered[-1]['pattern']} → {link[:60]}", emoji="✨")

        browser.close()

    log_debug(f"discover_url_patterns: итого {len(discovered)} новых паттернов")
    return discovered


# ─── Фильтрация и классификация ──────────────────────────────────────────────

def clean_urls(urls: list, domain: str) -> list:
    log_debug(f"clean_urls: start {len(urls)} URL, domain={domain}")
    skip_ext = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".pdf", ".zip",
                ".css", ".js", ".ico", ".webp", ".mp4", ".xml", ".gz",
                ".woff", ".woff2", ".ttf", ".eot"}
    seen = set()
    clean = []
    for u in urls:
        # Нормализуем: убираем trailing slash, query, fragment
        u = u.split("?")[0].split("#")[0].rstrip("/")
        if not u:
            continue
        # Нормализованный ключ для дедупликации
        u_lower = u.lower()
        if u_lower in seen:
            continue
        if domain not in u:
            continue
        if any(u_lower.endswith(ext) for ext in skip_ext):
            continue
        path = urlparse(u).path.lower()
        if re.search(r'/_next/|/static/|/assets/|/cdn-cgi/', path):
            continue
        seen.add(u_lower)
        clean.append(u)
    log_debug(f"clean_urls: {len(urls)} → {len(clean)} после фильтра/дедупа")
    return clean


def classify_all(urls: list, site_context: str = "", show_progress: bool = True, platform: str = "", skip_ai: bool = False) -> list:
    from page_classifier import classify_urls

    log_debug(f"classify_all: start {len(urls)} URL, platform={platform!r}, skip_ai={skip_ai}")
    # Дедуплицируем по path
    seen_paths = set()
    unique_urls = []
    for url in urls:
        path = urlparse(url).path.lower().rstrip("/") or "/"
        if path not in seen_paths:
            seen_paths.add(path)
            unique_urls.append(url)
    log_debug(f"classify_all: {len(urls)} → {len(unique_urls)} уникальных по path")

    # Классифицируем батчем — передаём платформу чтобы slug rules шли до Claude
    results_raw = classify_urls(unique_urls, site_context=site_context, show_progress=show_progress, platform=platform, skip_ai=skip_ai)

    results = []
    for url, c in zip(unique_urls, results_raw):
        if c is None:
            c = {"type": "general", "priority": 5, "method": "none"}
        results.append({
            "url": url,
            "path": urlparse(url).path or "/",
            **c,
        })
    return sorted(results, key=lambda x: (x["priority"], x["path"]))


def _struct_key(path: str) -> str:
    """Структурная форма URL = первый сегмент пути БЕЗ языкового префикса.
    /cars/altima → /cars; /en/robotics/arms → /robotics (иначе двуязычный
    сайт целиком схлопывается в одну форму '/en'). /en без хвоста остаётся
    /en — корень локализации, как в fast_classify/filter_lang_duplicates.
    # Tested: 2026-07-07 симуляция на 105 исторических step1.json — 83/105 бит-в-бит
    #         (nissan.ie идентичен); tinytronics: 1 форма/3 репа → 87 форм/123 репа.
    #         Известный wart: US-коды штатов /ar /de (homebuddy.com) тоже срезаются —
    #         даёт только сплиты (+репы), потерь покрытия нет."""
    parts = [p for p in path.lower().split("/") if p]
    if len(parts) > 1 and parts[0] in LANG_PREFIXES:
        parts = parts[1:]
    return "/" + parts[0] if parts else "/"


def collapse_by_form(classified_regex: list, reps_per_form: int = 3) -> tuple:
    """Схлопывает general-остаток по форме URL ДО дорогой Claude-классификации.

    Возвращает (protected, reps):
      protected = все POI (priority<=2) и discovered — НИКОГДА не схлопываем.
      reps      = <=reps_per_form представителей на форму из general-остатка
                  (priority>=3). Остальные siblings отбрасываются (они p5 general,
                  в скан не попадают).

    # Tested: 2026-06-26 on nissan.ie — Claude-вход 9280 → 43 URL (181 батч → 1),
    # 4 regex-POI защищены, merge POI 4→4, структура цела.
    """
    protected, reps, seen = [], [], {}
    for item in classified_regex:
        if item.get("priority", 5) <= 2 or item.get("discovered"):
            protected.append(item)
            continue
        struct = _struct_key(item.get("path", "/"))
        n = seen.get(struct, 0)
        if n < reps_per_form:
            reps.append(item)
            seen[struct] = n + 1
    return protected, reps


# ─── Вывод ───────────────────────────────────────────────────────────────────

TYPE_LABELS = {
    "lead_form":       "Lead form / contact",
    "booking_confirm": "Booking confirm",
    "quote":           "Quote / estimate",
    "checkout":        "Checkout / payment",
    "homepage":        "Homepage",
    "pricing":         "Pricing / plans",
    "location":        "Location / city page",
    "product":         "Product / listing detail",
    "use_case":        "Use case / industry",
    "search_results":  "Search / browse results",
    "faq_support":     "FAQ / help / guides",
    "careers":         "Careers / jobs",
    "about":           "About / company",
    "blog_content":    "Blog / content / media",
    "legal":           "Legal / policy",
    "technical":       "Technical / API",
    "reference":       "Reference / manual (bulk)",
    "general":         "General page",
}


def print_classified_list(classified: list, show_all: bool = False):
    log_debug(f"print_classified_list: {len(classified)} элементов, show_all={show_all}")
    by_type = {}
    for item in classified:
        by_type.setdefault(item["type"], []).append(item)

    poi    = [x for x in classified if x["priority"] <= 2]
    medium = [x for x in classified if x["priority"] == 3]
    skip   = [x for x in classified if x["priority"] >= 4]

    print(f"\n{'─' * 65}")
    print(f"  {'TYPE':<28} {'PRIORITY':<12} {'COUNT':>5}")
    print(f"{'─' * 65}")

    type_order = ["lead_form", "booking_confirm", "quote", "checkout",
                  "homepage", "pricing", "location", "product", "use_case",
                  "search_results", "faq_support", "careers", "about",
                  "blog_content", "legal", "technical", "reference", "general"]

    for ptype in type_order:
        items = by_type.get(ptype, [])
        if not items:
            continue
        label = get_page_priority_label(items[0]["priority"])
        desc  = TYPE_LABELS.get(ptype, ptype)
        print(f"\n  {desc:<28} {label:<12} {len(items):>3}")
        limit = 999 if show_all else 5
        for item in items[:limit]:
            disc = " ✨" if item.get("discovered") else ""
            print(f"    {item['path']}{disc}")
        if not show_all and len(items) > 5:
            print(f"    ... и ещё {len(items) - 5}")

    print(f"\n{'═' * 65}")
    print(f"  ИТОГО:          {len(classified)} страниц")
    print(f"  🔴🟠 POI:        {len(poi)} (будут сканироваться)")
    print(f"  🟡 Medium:       {len(medium)}")
    print(f"  🟢⚪ Low/Skip:   {len(skip)}")
    print(f"{'═' * 65}")


def ask_confirmation_smart(pages: list, limit: int) -> list:
    """Всегда спрашивает подтверждение. Показывает варианты."""
    n = len(pages)
    log_warn(f"К сканированию: {n} страниц")
    print(f"\n   Варианты:")
    print(f"   [1] Сканировать все {n} страниц — рекомендуется")
    if n > 10:
        print(f"   [2] Только первые 10")
    print(f"   [3] Ввести своё число")

    choice = input("\n   Выбор: ").strip()
    if choice == "1" or choice == "":
        return pages
    elif choice == "2" and n > 10:
        return pages[:10]
    elif choice == "3":
        try:
            num = int(input("   Сколько страниц: ").strip())
            return pages[:num]
        except ValueError:
            return pages
    return pages



# ─── Language duplicate filter ────────────────────────────────────────────────

LANG_PREFIXES = ("fr", "en", "de", "es", "it", "nl", "pt", "ru", "zh", "ja", "ko", "ar",
                 "fr-ca", "fr-be", "en-ca", "en-gb", "en-us", "zh-cn", "zh-tw")

def filter_lang_duplicates(urls: list) -> tuple:
    """
    Если сайт двуязычный (есть и /fr/... и /... без префикса) —
    убираем все URL с языковым префиксом, оставляем только EN версию.
    Возвращает (filtered_urls, removed_count, detected_lang_prefixes).
    """
    # Определяем какие языковые префиксы присутствуют
    from urllib.parse import urlparse
    log_debug(f"filter_lang_duplicates: start {len(urls)} URL")
    paths = [urlparse(u).path.lower() for u in urls]

    found_prefixes = set()
    for path in paths:
        for prefix in LANG_PREFIXES:
            if path.startswith(f"/{prefix}/") or path == f"/{prefix}":
                found_prefixes.add(prefix)

    if not found_prefixes:
        log_debug("filter_lang_duplicates: языковых префиксов не найдено — сайт одноязычный")
        return urls, 0, set()
    log_debug(f"filter_lang_duplicates: найдены префиксы {sorted(found_prefixes)}")

    # Проверяем — есть ли EN (без префикса) версии тех же страниц
    # Берём пути без префикса
    paths_no_prefix = set()
    for path in paths:
        stripped = re.sub(r'^/[a-z]{2}(?:-[a-z]{2,4})?(?=/)', '', path)
        if stripped != path:  # был префикс
            paths_no_prefix.add(stripped)

    # Есть ли EN аналоги? Считаем overlap
    en_paths = {p for p in paths if not any(p.startswith(f"/{px}/") or p == f"/{px}" for px in LANG_PREFIXES)}
    overlap = paths_no_prefix & en_paths

    # Если overlap >= 30% — считаем что EN версия существует, убираем lang дубли
    if not paths_no_prefix or len(overlap) / len(paths_no_prefix) < 0.3:
        log_debug(f"filter_lang_duplicates: overlap {len(overlap)}/{len(paths_no_prefix) or 0} < 30% — не убираем дубли")
        return urls, 0, set()
    log_debug(f"filter_lang_duplicates: overlap {len(overlap)}/{len(paths_no_prefix)} >= 30% — убираем lang-дубли")

    filtered = []
    removed = 0
    for u in urls:
        path = urlparse(u).path.lower()
        is_lang_url = any(path.startswith(f"/{px}/") or path == f"/{px}" for px in found_prefixes)
        if is_lang_url:
            removed += 1
        else:
            filtered.append(u)

    return filtered, removed, found_prefixes


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(domain: str, limit: int = DEFAULT_LIMIT, force_all: bool = False,
        show_all_in_list: bool = False, skip_discovery: bool = False) -> dict:

    base_url = ("https://" + domain if not domain.startswith("http") else domain).rstrip("/")
    site_domain = urlparse(base_url).netloc
    log_debug(f"run: start domain={domain} base_url={base_url} limit={limit} force_all={force_all} skip_discovery={skip_discovery}")

    log_header("TNC Pipeline — Step 1: Sitemap")
    log_info(f"Target: {base_url}")

    # Громко и сразу: без ключа половина классификации молча падает в general
    # (kogerstaete 2026-07-09: 19 из 22 URL — general, в скан попали 2 страницы).
    if not ANTHROPIC_API_KEY:
        log_warn("ANTHROPIC_API_KEY не найден (окружение/.env) — Claude-классификатор отключён, нераспознанные URL уйдут в general")

    # ── Шаг 1: sitemap ───────────────────────────────────────────
    log_step("Fetching sitemap...", emoji="📋")
    raw_urls, source = get_sitemap_urls(base_url)
    urls = clean_urls(raw_urls, site_domain)
    log_success(f"Источник: {source}")
    log_success(f"Найдено URL: {len(urls)}")

    # Фильтр языковых дублей ПЕРЕНЕСЁН ниже, за Language Detection: он всегда
    # оставлял no-prefix версию, считая её английской — для NL-default сайтов
    # (kogerstaete) это ровно наоборот. Решение о стороне теперь принимает развилка.

    # ── Соцсети + Facebook Ads Library ──────────────────────────
    log_step("Извлекаю соцсети и проверяю Facebook...", emoji="🌐")

    _homepage_html = ""
    _homepage_headers = {}
    _homepage_status = None
    _homepage_final_url = base_url  # финальный URL после редиректов — нужен языковой развилке (алиасы типа fritz-kola.de)
    try:
        from social_extractor import extract_socials, get_social_display
        import requests as _req
        _r = _req.get(base_url, headers=HEADERS, timeout=10)
        _homepage_html = _r.text
        _homepage_headers = dict(_r.headers)
        _homepage_status = _r.status_code
        _homepage_final_url = _r.url
        log_debug(f"run: homepage fetch ок, статус={_r.status_code}, html len={len(_homepage_html)}")
        socials_raw = extract_socials(_homepage_html, site_domain)
        log_debug(f"run: extract_socials вернул {len(socials_raw)} платформ")
    except Exception as e:
        socials_raw = {}
        log_warn(f"Ошибка парсинга соцсетей: {e}")

    # Старый формат для совместимости с остальным кодом
    social = {}
    found_any = False
    for platform in ["facebook", "instagram", "linkedin", "tiktok", "youtube", "twitter"]:
        if platform in socials_raw:
            item = socials_raw[platform]
            social[platform] = item["url"]
            if item.get("uncertain"):
                log_warn(f"{platform:<12} {item['url']}  ← НЕ УВЕРЕН (не совпадает с брендом)")
            else:
                log_success(f"{platform:<12} {item['url']}")
            found_any = True

    if not found_any:
        log_info(f"Соцсети не найдены на сайте")

    # Facebook — полная проверка через fb_page_id модуль
    fb_data = {"accounts": []}
    try:
        from fb_page_id import run as fb_run
        log_debug(f"run: вызываю fb_page_id.run({base_url}) — передаю уже скачанный homepage")
        fb_result = fb_run(base_url, html=(_homepage_html or None),
                           headers=(_homepage_headers or None), status=_homepage_status)
        fb_data = fb_result if fb_result else {"accounts": []}
        log_debug(f"run: fb_page_id вернул {len(fb_data.get('accounts', []))} аккаунтов")

        for acc in fb_data.get("accounts", []):
            if not acc.get("alive"):
                log_debug(f"run: FB-аккаунт {acc.get('handle')} не alive — пропуск")
                continue
            display_name = acc.get("display_name", acc.get("handle"))
            count = acc.get("active_ads_count")
            ads_url = acc.get("ads_library", {}).get("ALL", {}).get("active_only", "")
            partnership = acc.get("partnership_ads", False)
            partnership_n = acc.get("partnership_count", 0)
            n_texts = len(acc.get("ad_texts", []))
            n_images = len(acc.get("saved_images", []))

            if count and count > 0:
                log_success(f"{display_name} | ✅ {count} АКТИВНЫХ ОБЪЯВЛЕНИЙ")
                log_info(f"{ads_url}", emoji="📢")
                if partnership:
                    log_info(f"Partnership ads: ~{partnership_n}", emoji="🤝")
                if n_texts:
                    log_info(f"Текстов: {n_texts}", emoji="📝")
                if n_images:
                    log_info(f"Изображений: {n_images}", emoji="🖼")
            else:
                log_success(f"{display_name} | ❌ РЕКЛАМА НЕ КРУТИТСЯ")

    except ImportError:
        log_warn(f"fb_page_id.py не найден")

    social["facebook_accounts"] = fb_data

    # ── Platform Detection ──────────────────────────────────────
    log_step("Platform Detection...", emoji="🏗")
    platform_result = detect_platform(_homepage_html, _homepage_headers, urls)
    log_debug(f"run: detect_platform → {platform_result.get('platform')}")
    print_platform_result(platform_result)

    # ── Language Detection ──────────────────────────────────────
    lang_result = detect_site_language(_homepage_html, _homepage_headers)
    log_debug(f"run: detect_site_language → lang={lang_result['lang']} is_english={lang_result['is_english']} source={lang_result['source']}")
    # ── Языковая развилка (EN-first) ──────────────────────────────
    # EN-сайт → старый путь (фильтр дублей, оставляем no-prefix).
    # Не-EN → ищем EN-версию лесенкой language_detector; нашли → сканируем
    # ТОЛЬКО EN-дерево; не нашли → штатный стоп no_english_version.
    # Tested: 2026-07-10 — kogerstaete.nl: nl → switch на …/en (probe), to_scan 2 → 12 стр.
    #         (вкл. /en/rooms-suites/basic); americor/akvelon (EN): без switch, старый путь;
    #         tinytronics.nl: already-EN через Accept-Language, развилка не нужна;
    #         abianpaysbasque.fr (FR-only): стоп no_english_version, platform/FB сохранены;
    #         step2 по switched step1 сканит /en-страницы без правок.
    scan_base_url = base_url
    lang_versions = None
    lang_removed, lang_prefixes = 0, set()

    if lang_result["is_english"]:
        log_success(f"Язык сайта: {lang_result['lang'].upper()} (via {lang_result['source']})", emoji="🌐")
        urls, lang_removed, lang_prefixes = filter_lang_duplicates(urls)
        if lang_removed > 0:
            log_step(f"Двуязычный сайт — убраны {lang_removed} дублей на [{', '.join(sorted(lang_prefixes))}]", emoji="🌐")
            log_info(f"Сканируем только EN версию. Результаты применимы к обеим версиям.")
            log_info(f"Осталось URL: {len(urls)}")
    else:
        log_warn(f"Язык сайта: {lang_result['lang'].upper()} (via {lang_result['source']})")
        log_step("Сайт не на английском — ищу EN-версию...", emoji="🌐")
        lang_versions = discover_language_versions(
            _homepage_html, _homepage_headers, base_url, final_url=_homepage_final_url)
        if lang_versions["en_url"]:
            scan_base_url = lang_versions["en_url"]
            log_success(f"Найдена EN-версия: {scan_base_url} (источник: {lang_versions['source']}) — сканируем EN-дерево", emoji="🌐")
            urls, lang_removed = keep_lang_subtree(urls, scan_base_url)
            lang_prefixes = {"en"}
            if len(urls) < MIN_EN_POOL:
                log_info(f"EN-URL в пуле мало ({len(urls)}) — краулю EN-главную")
                extra = clean_urls(crawl_homepage_links(scan_base_url), site_domain)
                extra_en, _ = keep_lang_subtree(extra, scan_base_url)
                existing = set(urls)
                for u in extra_en:
                    if u not in existing:
                        urls.append(u)
                        existing.add(u)
                source += " + EN-tree crawl"
            if scan_base_url not in urls:
                urls.insert(0, scan_base_url)
            log_info(f"EN-пул: {len(urls)} URL (не-EN отброшено: {lang_removed})")
        else:
            log_error("Нет версии сайта на английском. Скан невозможен")
            if lang_versions["external_en"]:
                log_info(f"EN-версия существует на другом домене: {lang_versions['external_en']} — прогони её отдельным сканом", emoji="🌐")
            output = {
                "base_url": base_url,
                "status": "no_english_version",
                "status_message": "Нет версии сайта на английском. Скан невозможен",
                "sitemap_source": source,
                "platform": platform_result,
                "site_language": lang_result,
                "languages_available": lang_versions["versions"],
                "language_switch_source": None,
                "external_en": lang_versions["external_en"],
                "scan_url_base": None,
                "scan_language": None,
                "lang_removed": 0,
                "lang_prefixes": [],
                "total_found": 0,
                "discovered_patterns": 0,
                "social": social,          # platform/social/FB добыты ДО развилки — Ads-отчёт возможен
                "classified": [],
                "to_scan": [],
            }
            filename = scan_path(site_domain, f"{site_domain}_step1.json")
            with open(filename, "w", encoding="utf-8") as f:
                json.dump(output, f, indent=2, ensure_ascii=False)
            log_success(f"Сохранено: {filename}", emoji="💾")
            return output

    platform = platform_result.get("platform", "")

    # ── Шаг 2: предварительная regex-классификация (бесплатно) ─────
    # ОДИН проход только regex/patterns (skip_ai=True) по всем URL.
    # Раньше тут был полноценный classify_all с Claude API на ВЕСЬ sitemap,
    # и ещё раз ниже — итого дважды. На больших сайтах (Nissan, 9284 URL) это
    # уходило в сотни API-запросов и висло/падало ЕЩЁ ДО того, как срабатывал
    # лимит. Теперь дорогой Claude отложен до «ворот» (Шаг 3).
    classified = classify_all(urls, show_progress=False, platform=platform, skip_ai=True)
    poi_check = [x for x in classified if x["priority"] <= 2]
    log_debug(f"run: regex-POI={len(poi_check)} из {len(urls)} URL (Claude ещё не звали)")

    # ── Шаг 2b: pattern discovery если мало POI ───────────────────
    discovered_items = []
    if not skip_discovery:
        # Discovery нужен только при тонком sitemap. Большой sitemap (>= порога) =
        # хорошая coverage → не ползаем браузером, даже если POI мало (Nissan: POI=4,
        # но 9284 URL — рыть нечего).
        if len(urls) >= DISCOVERY_MAX_URLS:
            should_discover = False
            log_debug(f"run: sitemap большой ({len(urls)} >= {DISCOVERY_MAX_URLS}) — discovery пропущен")
        else:
            should_discover = (
                len(poi_check) < 5 or
                "fallback" in source or
                len(urls) < 20
            )
        log_debug(f"run: pre-check POI={len(poi_check)} urls={len(urls)} source={source!r} → should_discover={should_discover}")

        if should_discover:
            log_step(f"Мало POI ({len(poi_check)}) или ненадёжный источник — запускаю Pattern Discovery...", emoji="🔍")
            discovered = discover_url_patterns(scan_base_url, site_domain, urls)

            # В EN-режиме discovery ползает по EN-страницам, но в их шапках живут
            # ссылки на не-EN версии — отсекаем всё вне EN-дерева.
            if discovered and scan_base_url != base_url:
                kept_urls = set(keep_lang_subtree([d["url"] for d in discovered], scan_base_url)[0])
                dropped_n = len(discovered) - len(kept_urls)
                discovered = [d for d in discovered if d["url"] in kept_urls]
                if dropped_n:
                    log_debug(f"run: discovery — {dropped_n} не-EN URL отброшено subtree-фильтром")

            if discovered:
                # Дедуплицируем по структурному паттерну — берём только 1 пример каждого
                seen_patterns = set()
                unique_discovered = []
                for item in discovered:
                    # Структурный паттерн = первые два сегмента пути
                    path = urlparse(item["url"]).path
                    segments = [s for s in path.split("/") if s]
                    struct = "/" + "/".join(segments[:2]) if len(segments) >= 2 else path
                    if struct not in seen_patterns:
                        seen_patterns.add(struct)
                        unique_discovered.append(item)
                        log_success(f"Новый паттерн: {struct} → {item['url'][:60]}", emoji="✨")

                log_success(f"Найдено новых паттернов: {len(unique_discovered)}")
                existing = set(urls)
                for item in unique_discovered:
                    if item["url"] not in existing:
                        urls.append(item["url"])
                        existing.add(item["url"])
                        discovered_items.append(item)
                # discovery добавил URL — пересчитываем regex-POI (бесплатно)
                if discovered_items:
                    classified = classify_all(urls, show_progress=False, platform=platform, skip_ai=True)
                    poi_check = [x for x in classified if x["priority"] <= 2]
                    log_debug(f"run: после discovery regex-POI={len(poi_check)} из {len(urls)} URL")
            else:
                log_info(f"Новых паттернов не обнаружено")
    else:
        log_debug("run: skip_discovery=True — pattern discovery пропущен")

    # ── Шаг 3: ВОРОТА — нужен ли Claude вообще? ───────────────────
    # regex уже нашёл POI бесплатно (poi_check). Сканировать будем максимум
    # `limit` страниц, отсортированных по приоритету. Если regex дал POI >= limit,
    # Claude уже нечего добавить в скан → НЕ зовём дорогой API.
    log_step("Классификация страниц...", emoji="🏷")
    if len(poi_check) >= limit:
        log_success(f"Regex нашёл {len(poi_check)} POI (≥ лимита {limit}) — Claude API не нужен")
        # `classified` уже regex-only, используем как есть
    else:
        # POI мало → нужен Claude. Но СНАЧАЛА схлопываем формы по структуре URL,
        # чтобы в нейронку ушли только представители (~десятки), а не весь шум
        # (на Nissan это 9043 одинаковых страниц моделей → зависание).
        protected, reps = collapse_by_form(classified)
        dropped = len(classified) - len(protected) - len(reps)
        log_info(f"Regex нашёл {len(poi_check)} POI (< лимита {limit}) — дозапрашиваю Claude")
        log_step(f"Схлопнул {dropped} general-дублей по форме → {len(reps)} представителей; Claude увидит {len(reps)} URL", emoji="🔧")
        claude_results = classify_all([r["url"] for r in reps], show_progress=False, platform=platform, skip_ai=False)
        classified = sorted(protected + claude_results, key=lambda x: (x["priority"], x["path"]))

    # ── Platform-aware reclassification ─────────────────────────
    if platform_result["platform"] == "shopify":
        log_debug("run: shopify — применяю platform-aware reclassification к /pages/")
        for item in classified:
            path = item.get("path", "")
            if not path.startswith("/pages/"):
                continue
            current_method = item.get("method", "")
            current_type   = item.get("type", "general")
            # Slug rules применяем если:
            # - не распознан regex/patterns (всегда)
            # - Claude сказал general/technical — slug rules могут уточнить
            # - Claude сказал что-то конкретное — НЕ перезаписываем
            should_reclassify = (
                current_method not in ("patterns_json", "regex") and
                (current_method != "claude" or current_type in ("general", "technical"))
            )
            if should_reclassify:
                slug = path.replace("/pages/", "").lstrip("/").split("/")[0]
                reclassified = classify_shopify_page(slug)
                if reclassified is not None:
                    log_debug(f"run: shopify reclass {path}: {current_type} → {reclassified['type']} (slug={slug})")
                    item["type"] = reclassified["type"]
                    item["priority"] = reclassified["priority"]
                    item["method"] = reclassified["method"]

    # Помечаем discovered страницы
    disc_urls = {d["url"] for d in discovered_items}
    for item in classified:
        if item["url"] in disc_urls:
            item["discovered"] = True
            disc = next((d for d in discovered_items if d["url"] == item["url"]), {})
            item["discovery_source"] = disc.get("source", "")
            item["discovery_pattern"] = disc.get("pattern", "")

    # EN-root — принудительно homepage: classify_url не считает /en главной
    # (языковой префикс без следующего сегмента не стрипается, homepage-правило — это "/")
    if scan_base_url != base_url:
        for item in classified:
            if item["url"].rstrip("/") == scan_base_url.rstrip("/"):
                log_debug(f"run: EN-root {item['url']} принудительно homepage (был {item['type']}/{item['priority']})")
                item["type"], item["priority"], item["method"] = "homepage", 1, "lang_root"
                break

    # Итоговая статистика классификации
    n_regex    = sum(1 for x in classified if x.get("method") in ("regex", "patterns_json"))
    n_claude   = sum(1 for x in classified if x.get("method") == "claude")
    n_disc     = len(discovered_items)

    log_step("Итог классификации:", emoji="📊")
    log_info(f"Страниц из sitemap:       {len(urls) - n_disc}")
    if n_disc:
        log_info(f"Страниц из discovery:     {n_disc}")
    log_info(f"Опознано через Regex:     {n_regex}")
    if n_claude:
        log_info(f"Опознано через Claude AI: {n_claude}")

    poi_list = [x for x in classified if x["priority"] <= 2]
    print_classified_list(classified, show_all=show_all_in_list)

    # ── Умная дедупликация — по одному примеру каждого структурного паттерна
    def smart_deduplicate(pages: list) -> list:
        take_all_types = {"homepage", "lead_form", "booking_confirm", "quote",
                          "checkout", "pricing", "location", "use_case", "search_results"}
        result = []
        seen_patterns = {}
        for page in pages:
            ptype = page.get("type", "general")
            path = page.get("path", "")
            if ptype in take_all_types:
                result.append(page)
                continue
            struct = _struct_key(path)
            key = (ptype, struct)
            count = seen_patterns.get(key, 0)
            if count < 2:
                result.append(page)
                seen_patterns[key] = count + 1
        return result

    # Сначала дедупликация
    poi_deduped = smart_deduplicate(poi_list)
    saved = len(poi_list) - len(poi_deduped)
    if saved > 0:
        log_step(f"Дедупликация: {len(poi_list)} → {len(poi_deduped)} страниц (убрано {saved} дублей паттернов)", emoji="🔧")

    # Потом лимит и подтверждение
    if force_all:
        log_debug(f"run: force_all=True — берём все {len(poi_deduped)} страниц")
        to_scan = poi_deduped
    elif len(poi_deduped) <= limit:
        to_scan = poi_deduped
        log_success(f"{len(poi_deduped)} страниц к сканированию")
    else:
        log_warn(f"{len(poi_deduped)} страниц после дедупликации (лимит: {limit})")
        print(f"\n   Варианты:")
        print(f"   [1] Сканировать все {len(poi_deduped)} страниц — рекомендуется")
        print(f"   [2] Первые {limit} страниц")
        print(f"   [3] Ввести своё число")
        choice = input("\n   Выбор [1/2/3]: ").strip()
        log_debug(f"run: лимит-выбор пользователя = {choice!r}")
        if choice == "1":
            to_scan = poi_deduped
        elif choice == "2":
            to_scan = poi_deduped[:limit]
        elif choice == "3":
            try:
                n = int(input("   Сколько страниц: ").strip())
                to_scan = poi_deduped[:n]
            except ValueError as e:
                log_debug(f"run: невалидное число ({e}) — fallback на лимит {limit}")
                to_scan = poi_deduped[:limit]
        else:
            to_scan = poi_deduped

    log_step(f"К сканированию: {len(to_scan)} страниц", emoji="📌")

    # ── Сохраняем ────────────────────────────────────────────────
    output = {
        "base_url": base_url,
        "status": "ok",
        "scan_url_base": scan_base_url,
        "scan_language": "en" if scan_base_url != base_url else lang_result["lang"],
        "languages_available": (lang_versions or {}).get("versions", {}),
        "language_switch_source": (lang_versions or {}).get("source"),
        "external_en": (lang_versions or {}).get("external_en"),
        "sitemap_source": source,
        "platform": platform_result,
        "site_language": lang_result,
        "lang_removed": lang_removed,
        "lang_prefixes": sorted(lang_prefixes),
        "total_found": len(urls),
        "discovered_patterns": len(discovered_items),
        "social": social,
        "classified": classified,
        "to_scan": to_scan,
    }

    filename = scan_path(site_domain, f"{site_domain}_step1.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    log_success(f"Сохранено: {filename}", emoji="💾")

    # ── Интерактивное обучение ────────────────────────────────────
    try:
        from learn import print_classification_report, interactive_learn
        print_classification_report(classified, site_domain)
        interactive_learn(classified, site_domain)
    except ImportError:
        log_info(f"learn.py не найден — пропускаем обучение")

    # ── Следующий шаг — в самом конце ────────────────────────────
    step1_file = scan_path(site_domain, f"{site_domain}_step1.json")
    log_header("💡 Следующий шаг")
    log_info(f"python step2_scan.py {step1_file}")

    return output


if __name__ == "__main__":
    from utils import setup_console
    setup_console()  # UTF-8 до первого вывода: русские help-строки argparse падали на cp1252 при --help
    # Tested: 2026-07-09 --help под PYTHONIOENCODING=cp1252 — help печатается, exit 0
    parser = argparse.ArgumentParser(description="TNC Step 1 — Sitemap Fetch & Classify")
    parser.add_argument("domain", help="Domain (e.g. bandago.com)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Лимит страниц до подтверждения (default: {DEFAULT_LIMIT})")
    parser.add_argument("--all", action="store_true", help="Сканировать все без вопросов")
    parser.add_argument("--show-all", action="store_true", help="Показать все URL в списке")
    parser.add_argument("--no-discovery", action="store_true", help="Отключить pattern discovery")
    parser.add_argument("--debug", action="store_true", help="Полный отладочный лог (DEBUG — теперь это дефолт)")
    parser.add_argument("--fire", action="store_true", help="Firehose: построчный лог по каждому URL/ссылке (уровень FIRE, глубже DEBUG)")
    parser.add_argument("--quiet", action="store_true", help="Приглушить: показывать только INFO+ (скрыть DEBUG)")
    args = parser.parse_args()

    # Logging
    from utils import setup_logging, normalize_url
    import log
    if args.fire:
        log.set_level("FIRE")
    elif args.debug:
        log.set_level("DEBUG")
    if args.quiet:
        log.set_level("INFO")
    _log_path = setup_logging(args.domain, step="step1")

    run(args.domain, limit=args.limit, force_all=args.all,
        show_all_in_list=args.show_all, skip_discovery=args.no_discovery)

    # Сообщение пишется в лог и в консоль
    log_info(f"Лог: {_log_path}", emoji="📝")
