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
    print("❌ Установи: pip install requests")
    sys.exit(1)

from page_classifier import classify_url, get_page_priority_label, save_pattern, load_patterns
from platform_detector import detect_platform, print_platform_result, classify_shopify_page
from utils import get_scan_dir, scan_path, TeeLogger, setup_logging, HEADERS, safe_get, normalize_url, detect_site_language


DEFAULT_LIMIT = 65


# ─── Sitemap helpers ──────────────────────────────────────────────────────────

def looks_like_sitemap_content(text: str) -> bool:
    return "<loc>" in text or "sitemapindex" in text.lower() or "urlset" in text.lower()


def fetch_urls_from_xml(xml_text: str) -> list:
    urls = []
    try:
        root = ET.fromstring(xml_text)
        ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        for loc in root.findall(".//sm:loc", ns):
            u = loc.text.strip() if loc.text else ""
            if u:
                urls.append(u)
    except ET.ParseError:
        urls = re.findall(r'<loc>\s*(https?://[^\s<]+)\s*</loc>', xml_text)
    return urls


def expand_sitemap_index(urls: list) -> list:
    expanded = []
    for u in urls:
        if re.search(r'sitemap', u, re.IGNORECASE) or u.endswith(".xml"):
            try:
                r = requests.get(u, headers=HEADERS, timeout=10)
                if r.status_code == 200 and looks_like_sitemap_content(r.text):
                    sub = fetch_urls_from_xml(r.text)
                    for su in sub:
                        if re.search(r'sitemap', su, re.IGNORECASE) or su.endswith(".xml"):
                            try:
                                r2 = requests.get(su, headers=HEADERS, timeout=10)
                                if r2.status_code == 200:
                                    expanded.extend(fetch_urls_from_xml(r2.text))
                            except Exception:
                                pass
                        else:
                            expanded.append(su)
                    continue
            except Exception:
                pass
        expanded.append(u)
    return expanded


def try_fetch_sitemap(url: str) -> list:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10, allow_redirects=True)
        if r.status_code != 200:
            return []
        if not looks_like_sitemap_content(r.text):
            return []
        urls = fetch_urls_from_xml(r.text)
        urls = expand_sitemap_index(urls)
        return urls
    except Exception:
        return []


def crawl_homepage_links(base_url: str) -> list:
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=10)
        domain = urlparse(base_url).netloc
        hrefs = re.findall(r'href=["\']([^"\'#]+)["\']', r.text)
        urls = set()
        for h in hrefs:
            h = h.split("?")[0].split("#")[0]
            if h.startswith("/") and len(h) > 1:
                urls.add(urljoin(base_url, h))
            elif domain in h:
                urls.add(h)
        return list(urls)
    except Exception:
        return []


def get_sitemap_urls(base_url: str) -> tuple:
    """
    Умный поиск sitemap — 4 уровня:
    1. Стандартные пути
    2. robots.txt
    3. Ссылки с 'sitemap' на главной
    4. Fallback — href с главной
    """
    standard_paths = [
        "/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
        "/sitemap.xml.gz", "/wp-sitemap.xml", "/sitemap/", "/sitemap",
        "/sitemaps/sitemap.xml", "/news-sitemap.xml", "/pages-sitemap.xml",
        "/post-sitemap.xml",
    ]

    for path in standard_paths:
        urls = try_fetch_sitemap(base_url + path)
        if urls:
            return urls, base_url + path

    try:
        r = requests.get(f"{base_url}/robots.txt", headers=HEADERS, timeout=10)
        if r.status_code == 200:
            sm_urls = re.findall(r'(?i)sitemap\s*:\s*(https?://\S+)', r.text)
            for sm_url in sm_urls:
                urls = try_fetch_sitemap(sm_url)
                if urls:
                    return urls, f"robots.txt → {sm_url}"
    except Exception:
        pass

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
        for candidate in sorted(candidates):
            urls = try_fetch_sitemap(candidate)
            if urls:
                return urls, f"discovered → {candidate}"
    except Exception:
        pass

    urls = crawl_homepage_links(base_url)
    return urls, "homepage crawl (fallback)"


# ─── Pattern Discovery через Playwright ──────────────────────────────────────

def discover_url_patterns(base_url: str, domain: str, known_urls: list) -> list:
    """
    Открывает сайт браузером, находит структурные паттерны URL
    которых нет в sitemap И не покрыты классификатором.
    Возвращает по одному примеру каждого нового паттерна.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
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

    discovered = []
    seen_patterns = set()  # паттерны которые уже добавили

    print(f"  🔍 Pattern discovery: открываю браузер...")

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
            return False

        # Проверяем структурный паттерн через classifier
        # Используем корневой URL паттерна, не конкретный продукт
        pattern_url = f"https://{domain}{pattern}"
        test_result = classify_url(pattern_url)
        if test_result["type"] != "general":
            seen_patterns.add(pattern)
            return False

        # Новый паттерн которого классификатор не знает
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
        )
        page = context.new_page()

        pages_to_explore = [base_url]
        for u in known_urls[:10]:
            if classify_url(u)["priority"] <= 2:
                pages_to_explore.append(u)

        explored = set()

        for explore_url in pages_to_explore[:5]:
            if explore_url in explored:
                continue
            explored.add(explore_url)

            try:
                page.goto(explore_url, wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(2000)
            except Exception:
                try:
                    page.goto(explore_url, wait_until="domcontentloaded", timeout=10000)
                    page.wait_for_timeout(1500)
                except Exception:
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
                except Exception:
                    pass

            for link in all_links:
                if process_link(link):
                    print(f"    ✨ Новый паттерн: {discovered[-1]['pattern']} → {link[:60]}")

        browser.close()

    return discovered


# ─── Фильтрация и классификация ──────────────────────────────────────────────

def clean_urls(urls: list, domain: str) -> list:
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
    return clean


def classify_all(urls: list, site_context: str = "", show_progress: bool = True, platform: str = "") -> list:
    from page_classifier import classify_urls
    
    # Дедуплицируем по path
    seen_paths = set()
    unique_urls = []
    for url in urls:
        path = urlparse(url).path.lower().rstrip("/") or "/"
        if path not in seen_paths:
            seen_paths.add(path)
            unique_urls.append(url)

    # Классифицируем батчем — передаём платформу чтобы slug rules шли до Claude
    results_raw = classify_urls(unique_urls, site_context=site_context, show_progress=show_progress, platform=platform)

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
    "general":         "General page",
}


def print_classified_list(classified: list, show_all: bool = False):
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
                  "blog_content", "legal", "technical", "general"]

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
    print(f"\n⚠️  К сканированию: {n} страниц")
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



# ─── Main ─────────────────────────────────────────────────────────────────────

def run(domain: str, limit: int = DEFAULT_LIMIT, force_all: bool = False,
        show_all_in_list: bool = False, skip_discovery: bool = False) -> dict:

    base_url = ("https://" + domain if not domain.startswith("http") else domain).rstrip("/")
    site_domain = urlparse(base_url).netloc

    print(f"\n{'═' * 65}")
    print(f"  TNC Pipeline — Step 1: Sitemap")
    print(f"  Target: {base_url}")
    print(f"{'═' * 65}")

    # ── Шаг 1: sitemap ───────────────────────────────────────────
    print(f"\n📋 Fetching sitemap...")
    raw_urls, source = get_sitemap_urls(base_url)
    urls = clean_urls(raw_urls, site_domain)
    print(f"  ✓ Источник: {source}")
    print(f"  ✓ Найдено URL: {len(urls)}")

    # ── Соцсети + Facebook Ads Library ──────────────────────────
    print(f"\n🌐 Извлекаю соцсети и проверяю Facebook...")
    
    _homepage_html = ""
    _homepage_headers = {}
    try:
        from social_extractor import extract_socials, get_social_display
        import requests as _req
        _r = _req.get(base_url, headers=HEADERS, timeout=10)
        _homepage_html = _r.text
        _homepage_headers = dict(_r.headers)
        socials_raw = extract_socials(_homepage_html, site_domain)
    except Exception as e:
        socials_raw = {}
        print(f"  ⚠️  Ошибка парсинга соцсетей: {e}")

    # Старый формат для совместимости с остальным кодом
    social = {}
    found_any = False
    for platform in ["facebook", "instagram", "linkedin", "tiktok", "youtube", "twitter"]:
        if platform in socials_raw:
            item = socials_raw[platform]
            social[platform] = item["url"]
            if item.get("uncertain"):
                print(f"  ⚠️  {platform:<12} {item['url']}  ← НЕ УВЕРЕН (не совпадает с брендом)")
            else:
                print(f"  ✓ {platform:<12} {item['url']}")
            found_any = True

    if not found_any:
        print(f"  ℹ️  Соцсети не найдены на сайте")

    # Facebook — полная проверка через fb_page_id модуль
    fb_data = {"accounts": []}
    try:
        from fb_page_id import (
            check_fb_page_alive_playwright, build_ads_library_urls, get_active_ads_count
        )

        # Берём FB URL из social_extractor — уже чистый, без дублей
        fb_handles = []
        if "facebook" in socials_raw:
            fb_item = socials_raw["facebook"]
            fb_handles.append({
                "handle": fb_item["handle"],
                "url": fb_item["url"],
                "format": fb_item.get("type", "vanity"),
            })
        if "facebook_all" in socials_raw:
            seen = {fb_handles[0]["handle"]} if fb_handles else set()
            for extra in socials_raw["facebook_all"][1:]:
                if extra["handle"] not in seen:
                    seen.add(extra["handle"])
                    fb_handles.append({
                        "handle": extra["handle"],
                        "url": extra["url"],
                        "format": extra.get("type", "vanity"),
                    })

        if fb_handles:
            checked = []
            for item in fb_handles:
                full_path = item["url"].replace("https://www.facebook.com/", "")
                status = check_fb_page_alive_playwright(full_path)
                checked.append((item, status))

            live_handles = {
                item["handle"].lower().replace("_", "").replace("-", "")
                for item, status in checked if status["alive"]
            }

            for item, status in checked:
                handle = item["handle"]
                fmt = item.get("format", "vanity")
                handle_norm = handle.lower().replace("_", "").replace("-", "")

                if not status["alive"]:
                    if handle_norm in live_handles:
                        print(f"  ⚠️  Битая ссылка на сайте: facebook.com/{handle} (дубль живого аккаунта)")
                    else:
                        print(f"  ✗ {handle} — DEAD LINK")
                    fb_data["accounts"].append({
                        "handle": handle,
                        "url": item["url"],
                        "format": fmt,
                        "published": False,
                        "broken_reason": status["reason"],
                        "ads_library": None,
                        "active_ads_count": None,
                    })
                    continue

                # Display name из Playwright — то что Facebook показывает публично
                display_name = status.get("display_name") or handle

                ads_urls = build_ads_library_urls(display_name)
                ads_count = get_active_ads_count(display_name)
                count = ads_count.get("count")

                if count and count > 0:
                    print(f"  ✓ {display_name} | ✅ {count} АКТИВНЫХ ОБЪЯВЛЕНИЙ")
                    print(f"    📢 {ads_urls['ALL']['active_only']}")
                else:
                    print(f"  ✓ {display_name} | ❌ РЕКЛАМА НЕ КРУТИТСЯ")

                fb_data["accounts"].append({
                    "handle": handle,
                    "display_name": display_name,
                    "url": item["url"],
                    "format": fmt,
                    "published": True,
                    "active_ads_count": count,
                    "ads_library": ads_urls,
                })

    except ImportError:
        print(f"  ⚠️  fb_page_id.py не найден")

    social["facebook_accounts"] = fb_data

    # ── Platform Detection ──────────────────────────────────────
    print(f"\n🏗  Platform Detection...")
    platform_result = detect_platform(_homepage_html, _homepage_headers, urls)
    print_platform_result(platform_result)

    # ── Language Detection ──────────────────────────────────────
    lang_result = detect_site_language(_homepage_html, _homepage_headers)
    lang_emoji = "✅" if lang_result["is_english"] else "⚠️ "
    print(f"\n🌐 Язык сайта: {lang_emoji} {lang_result['lang'].upper()} (via {lang_result['source']})")
    if not lang_result["is_english"]:
        print(f"   ⚠️  Сайт не на английском языке.")
        print(f"      CTA-детектор (regex) и classify_page_content работают только для EN.")
        print(f"      Кнопки могут не распознаться. Результаты step2 менее надёжны.")

    # ── Шаг 2: pattern discovery если мало POI ───────────────────
    discovered_items = []
    if not skip_discovery:
        classified_check = classify_all(urls, show_progress=False)
        poi_check = [x for x in classified_check if x["priority"] <= 2]

        # Запускаем discovery если POI мало или это fallback
        should_discover = (
            len(poi_check) < 5 or
            "fallback" in source or
            len(urls) < 20
        )

        if should_discover:
            print(f"\n🔍 Мало POI ({len(poi_check)}) или ненадёжный источник — запускаю Pattern Discovery...")
            discovered = discover_url_patterns(base_url, site_domain, urls)

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
                        print(f"    ✨ Новый паттерн: {struct} → {item['url'][:60]}")

                print(f"  ✓ Найдено новых паттернов: {len(unique_discovered)}")
                existing = set(urls)
                for item in unique_discovered:
                    if item["url"] not in existing:
                        urls.append(item["url"])
                        existing.add(item["url"])
                        discovered_items.append(item)
            else:
                print(f"  ℹ️  Новых паттернов не обнаружено")

    # ── Шаг 3: классифицируем ────────────────────────────────────
    classified = classify_all(urls, show_progress=False, platform=platform_result.get("platform", ""))

    # ── Platform-aware reclassification ─────────────────────────
    if platform_result["platform"] == "shopify":
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

    # Итоговая статистика классификации
    n_regex    = sum(1 for x in classified if x.get("method") in ("regex", "patterns_json"))
    n_claude   = sum(1 for x in classified if x.get("method") == "claude")
    n_disc     = len(discovered_items)

    print(f"\n📊 Итог классификации:")
    print(f"   Страниц из sitemap:       {len(urls) - n_disc}")
    if n_disc:
        print(f"   Страниц из discovery:     {n_disc}")
    print(f"   Опознано через Regex:     {n_regex}")
    if n_claude:
        print(f"   Опознано через Claude AI: {n_claude}")

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
            parts = [p for p in path.split("/") if p]
            if ptype in take_all_types:
                result.append(page)
                continue
            struct = "/" + parts[0] if parts else path
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
        print(f"\n🔧 Дедупликация: {len(poi_list)} → {len(poi_deduped)} страниц (убрано {saved} дублей паттернов)")

    # Потом лимит и подтверждение
    if force_all:
        to_scan = poi_deduped
    elif len(poi_deduped) <= limit:
        to_scan = poi_deduped
        print(f"\n✅ {len(poi_deduped)} страниц к сканированию")
    else:
        print(f"\n⚠️  {len(poi_deduped)} страниц после дедупликации (лимит: {limit})")
        print(f"\n   Варианты:")
        print(f"   [1] Сканировать все {len(poi_deduped)} страниц — рекомендуется")
        print(f"   [2] Первые {limit} страниц")
        print(f"   [3] Ввести своё число")
        choice = input("\n   Выбор [1/2/3]: ").strip()
        if choice == "1":
            to_scan = poi_deduped
        elif choice == "2":
            to_scan = poi_deduped[:limit]
        elif choice == "3":
            try:
                n = int(input("   Сколько страниц: ").strip())
                to_scan = poi_deduped[:n]
            except ValueError:
                to_scan = poi_deduped[:limit]
        else:
            to_scan = poi_deduped

    print(f"\n📌 К сканированию: {len(to_scan)} страниц")

    # ── Сохраняем ────────────────────────────────────────────────
    output = {
        "base_url": base_url,
        "sitemap_source": source,
        "platform": platform_result,
        "site_language": lang_result,
        "total_found": len(urls),
        "discovered_patterns": len(discovered_items),
        "social": social,
        "classified": classified,
        "to_scan": to_scan,
    }

    filename = scan_path(site_domain, f"{site_domain}_step1.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"💾 Сохранено: {filename}")

    # ── Интерактивное обучение ────────────────────────────────────
    try:
        from learn import print_classification_report, interactive_learn
        print_classification_report(classified, site_domain)
        interactive_learn(classified, site_domain)
    except ImportError:
        print(f"\n  ℹ️  learn.py не найден — пропускаем обучение")

    # ── Следующий шаг — в самом конце ────────────────────────────
    step1_file = scan_path(site_domain, f"{site_domain}_step1.json")
    print(f"\n{'═' * 65}")
    print(f"  💡 Следующий шаг:")
    print(f"     python step2_scan.py {step1_file}")
    print(f"{'═' * 65}\n")

    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TNC Step 1 — Sitemap Fetch & Classify")
    parser.add_argument("domain", help="Domain (e.g. bandago.com)")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"Лимит страниц до подтверждения (default: {DEFAULT_LIMIT})")
    parser.add_argument("--all", action="store_true", help="Сканировать все без вопросов")
    parser.add_argument("--show-all", action="store_true", help="Показать все URL в списке")
    parser.add_argument("--no-discovery", action="store_true", help="Отключить pattern discovery")
    args = parser.parse_args()

    # Logging
    from utils import setup_logging, normalize_url
    _log_path = setup_logging(args.domain, step="step1")

    run(args.domain, limit=args.limit, force_all=args.all,
        show_all_in_list=args.show_all, skip_discovery=args.no_discovery)

    # Сообщение пишется в лог и в консоль
    print(f"\n📝 Лог: {_log_path}")
