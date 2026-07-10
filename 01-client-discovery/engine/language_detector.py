"""
TNC Pipeline — Language Version Detector
=========================================
Находит языковые версии сайта и подтверждённый EN-корень для EN-first стратегии
сканирования (кейс kogerstaete.nl: сканер заточен на EN, сайт по умолчанию NL).

Дизайн — по эмпирике 11 EU-сайтов (2026-07-10, workflow eu-multilang-research):
head-hreflang есть лишь у ~половины сайтов (нет у concrete5/WPML/Magento/IKEA/SPA),
поэтому лесенка источников от бесплатных к сетевым, стоп на первом подтверждённом:

    0. already_en      — мы уже на EN (Accept-Language negotiation средиректил)
    1. hreflang_html   — <link rel="alternate" hreflang> из уже скачанного HTML
    2. lang_links      — <a href> с /en/-префиксом, en.-субдомен, GET-переключатели
    3. hreflang_sitemap— xhtml:link из УЖЕ скачанного sitemap XML (сами не качаем)
    4. probe           — слепой GET /en/ (+/en-us/, /en-gb/, /{cc}/en/ для IKEA-паттерна)
    5. switcher        — переход по GET-endpoint переключателя (concrete5, 302)

Каждый кандидат подтверждается _confirm_en: 200 + тот же registrable-домен +
не схлопнулся в корень + html lang финальной страницы = en. Это закрывает обе
ловушки probe: false positive (fritz-kola.de/en/ → 301 → немецкий корень) и
доверие битым hreflang.

ccTLD-сиблинг (aerosus.de → EN на aerosus.com, другой registrable-домен) — НЕ
переключаемся, только помечаем в external_en (ломает keying scans/, вне скоупа).

Запуск (ручная проверка):
    python language_detector.py kogerstaete.nl
    python language_detector.py fritz-kola.com berlin.de migros.ch

# Tested: 2026-07-10 CLI по 10 сайтам исследования — kogerstaete.nl (probe → /en),
#         tinytronics.nl (already_en через Accept-Language), fritz-kola.com и .de
#         (hreflang_html → /en; алиас .de → .com следуется, false positive нет),
#         berlin.de (hreflang_html, относительный href), lesgrandsbuffets.com
#         (lang_links), victorinox.com и bandago.com (already_en),
#         aerosus.de (en_url=None + external_en=aerosus.com — ccTLD не переключаем),
#         migros.ch (SPA-shell → честный None, бэклог Googlebot-UA).
"""

import re
import sys
from urllib.parse import urljoin, urlparse

from utils import detect_site_language, HEADERS
from log import log_debug, log_info, log_warn

# Бюджеты сетевых запросов (детектор не должен раздувать скан)
MAX_CONFIRM_GETS = 8   # суммарный лимит подтверждающих GET на все источники
MAX_SWITCHER_FOLLOWS = 4

_TIMEOUT = 15


# ─── Мелкие помощники ─────────────────────────────────────────────────────────

def _registrable_domain(host: str) -> str:
    """kogerstaete.nl / www.kogerstaete.nl → kogerstaete.nl.
    Наивно (последние 2 лейбла) — для co.uk и т.п. даст 'co.uk'; для наших
    кейсов достаточно, полноценный PSL — не для v1."""
    h = host.lower().split(":")[0]
    if h.startswith("www."):
        h = h[4:]
    parts = h.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else h


def _same_site(host_a: str, host_b: str) -> bool:
    return _registrable_domain(host_a) == _registrable_domain(host_b)


def _pick_en(versions: dict):
    """Выбор EN-кандидата из карты lang→url: en → en-gb/en-us → любой en-*."""
    for key in ("en", "en-gb", "en-us"):
        if key in versions:
            return versions[key]
    for lang, url in versions.items():
        if lang.startswith("en-") or lang.startswith("en_"):
            return url
    return None


def _extract_attr(tag: str, attr: str):
    m = re.search(attr + r'\s*=\s*["\']([^"\']+)["\']', tag, re.IGNORECASE)
    return m.group(1).strip() if m else None


# ─── Источник 1: hreflang в HTML head ────────────────────────────────────────

def parse_hreflang_html(html: str, base_for_join: str):
    """Карта lang→абсолютный URL из <link rel="alternate" hreflang=...>.
    Атрибуты — в любом порядке (berlin.de: hreflang→href→rel), href бывает
    относительным (/en/) — резолвим против финального URL главной."""
    versions, x_default = {}, None
    for tag in re.findall(r"<link\b[^>]+>", html, re.IGNORECASE):
        if "alternate" not in tag.lower() or "hreflang" not in tag.lower():
            continue
        lang = _extract_attr(tag, "hreflang")
        href = _extract_attr(tag, "href")
        if not lang or not href:
            continue
        url = urljoin(base_for_join, href)
        if lang.lower() == "x-default":
            x_default = url
        else:
            versions[lang.lower()] = url
    return versions, x_default


# ─── Источник 2: языковые ссылки в HTML ──────────────────────────────────────

_EN_PREFIX_RE = re.compile(r"^/(en|en-[a-z]{2,4})(/|$)", re.IGNORECASE)
_SWITCHER_RE = re.compile(r"/switch_language/|[?&]lang(uage)?=en\b", re.IGNORECASE)

def find_lang_links(html: str, final_url: str):
    """Из <a href>: EN-корни на том же сайте, en.-субдомены, ccTLD-сиблинги,
    GET-endpoints переключателей. Современные переключатели часто href-less
    (POST-формы Shopify/OpenCart) — тогда тут пусто, идём ниже по лесенке."""
    fin = urlparse(final_url)
    home_reg = _registrable_domain(fin.netloc)
    home_label = home_reg.split(".")[0]

    prefix_roots, subdomains, cctld_siblings, switchers = [], [], [], []
    for m in re.finditer(r'<a\b[^>]+href\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE):
        absu = urljoin(final_url, m.group(1))
        p = urlparse(absu)
        if p.scheme not in ("http", "https"):
            continue
        if _SWITCHER_RE.search(absu) and _same_site(p.netloc, fin.netloc):
            if absu not in switchers:
                switchers.append(absu)
            continue
        if _same_site(p.netloc, fin.netloc):
            pm = _EN_PREFIX_RE.match(p.path)
            if pm:
                root = f"{p.scheme}://{p.netloc}/{pm.group(1)}"
                if root not in prefix_roots:
                    prefix_roots.append(root)
            if p.netloc.lower().startswith("en."):
                root = f"{p.scheme}://{p.netloc}/"
                if root not in subdomains:
                    subdomains.append(root)
        else:
            # ccTLD-сиблинг: тот же домен-лейбл, другая зона (aerosus.de → aerosus.com)
            sib_reg = _registrable_domain(p.netloc)
            if sib_reg.split(".")[0] == home_label and sib_reg != home_reg:
                if absu not in cctld_siblings:
                    cctld_siblings.append(absu)
    return prefix_roots, subdomains, cctld_siblings, switchers


# ─── Источник 3: hreflang в sitemap XML ──────────────────────────────────────

def parse_hreflang_sitemap(xml_text: str, sitemap_url: str):
    """xhtml:link hreflang из УЖЕ скачанного sitemap. href/loc бывают
    ОТНОСИТЕЛЬНЫМИ (kogerstaete — нарушение протокола, но реальность) —
    резолвим против URL самого sitemap. EN-корень = самый короткий en-путь."""
    en_urls = []
    for tag in re.findall(r"<xhtml:link\b[^>]+>", xml_text, re.IGNORECASE):
        lang = _extract_attr(tag, "hreflang")
        href = _extract_attr(tag, "href")
        if not lang or href is None:
            continue
        if lang.lower() == "en" or lang.lower().startswith("en-"):
            en_urls.append(urljoin(sitemap_url, href))
    if not en_urls:
        return None
    return min(en_urls, key=lambda u: len(urlparse(u).path))


# ─── Подтверждение кандидата ─────────────────────────────────────────────────

def _confirm_response(resp, home_host: str):
    """Общая проверка ответа: 200, тот же registrable-домен, не схлопнулось
    в корень (fritz-kola: /en/ → 301-цепочка → немецкий корень), финальный
    HTML реально английский. Возвращает en_home-словарь | "external" | None."""
    if resp is None or resp.status_code != 200:
        return None
    fin = urlparse(resp.url)
    if not _same_site(fin.netloc, home_host):
        return "external"
    lang = detect_site_language(resp.text, dict(resp.headers)).get("lang")
    if lang != "en":
        return None
    return {"url": resp.url.rstrip("/"), "html": resp.text,
            "headers": dict(resp.headers), "status": resp.status_code}


def _confirm_en(candidate_url: str, home_host: str, budget: dict):
    if budget["gets"] >= MAX_CONFIRM_GETS:
        log_debug(f"_confirm_en: бюджет {MAX_CONFIRM_GETS} GET исчерпан, пропускаю {candidate_url}")
        return None
    budget["gets"] += 1
    import requests
    try:
        r = requests.get(candidate_url, headers=HEADERS, timeout=_TIMEOUT, allow_redirects=True)
    except Exception as e:
        log_debug(f"_confirm_en: {candidate_url} → {type(e).__name__}")
        return None
    verdict = _confirm_response(r, home_host)
    log_debug(f"_confirm_en: {candidate_url} → {r.status_code} {r.url} → "
              f"{'OK' if isinstance(verdict, dict) else verdict}")
    return verdict


# ─── Главная функция ─────────────────────────────────────────────────────────

def discover_language_versions(html: str, headers: dict, base_url: str,
                               final_url: str = None, sitemap_xml: str = None,
                               sitemap_url: str = None, probe: bool = True) -> dict:
    """Лесенка источников EN-версии, стоп на первом подтверждённом.

    html/headers — уже скачанная главная (не качаем повторно), final_url — её
    финальный URL после редиректов (важно: fritz-kola.de живёт на fritz-kola.com).
    sitemap_xml/sitemap_url — сырой sitemap если step1 его уже скачал.
    """
    final_url = (final_url or base_url).rstrip("/") or base_url
    home_host = urlparse(final_url).netloc or urlparse(base_url).netloc

    lang_res = detect_site_language(html, headers)
    result = {
        "default_lang": lang_res["lang"],
        "versions": {},
        "en_url": None,
        "source": None,
        "x_default": None,
        "external_en": None,
        "en_home": None,
    }
    budget = {"gets": 0}

    # ── 0. already_en: мы уже на EN (Accept-Language negotiation, tinytronics) ──
    if lang_res["lang"] == "en":
        result["en_url"] = final_url
        result["source"] = "already_en"
        result["versions"]["en"] = final_url
        result["en_home"] = {"url": final_url, "html": html,
                             "headers": dict(headers or {}), "status": 200}
        log_debug("discover_language_versions: already_en — главная уже английская")
        return result

    # ── 1. hreflang в HTML head ──────────────────────────────────────────────
    versions, x_default = parse_hreflang_html(html, final_url)
    result["versions"].update(versions)
    result["x_default"] = x_default
    cand = _pick_en(versions)
    if cand:
        verdict = _confirm_en(cand, home_host, budget)
        if isinstance(verdict, dict):
            result.update(en_url=verdict["url"], source="hreflang_html", en_home=verdict)
            return result
        if verdict == "external":
            result["external_en"] = cand  # hreflang ведёт на чужой домен — не переключаемся

    # ── 2. языковые ссылки в HTML ────────────────────────────────────────────
    prefix_roots, subdomains, cctld_siblings, switchers = find_lang_links(html, final_url)
    if cctld_siblings and not result["external_en"]:
        # .com-сиблинг вероятнее всего международный/EN (aerosus.de: .net и .com в футере)
        cctld_siblings.sort(key=lambda u: 0 if urlparse(u).netloc.endswith(".com") else 1)
        result["external_en"] = cctld_siblings[0]
    for cand in (prefix_roots + subdomains)[:3]:
        verdict = _confirm_en(cand, home_host, budget)
        if isinstance(verdict, dict):
            result.update(en_url=verdict["url"], source="lang_links", en_home=verdict)
            return result

    # ── 3. hreflang в уже скачанном sitemap ──────────────────────────────────
    if sitemap_xml:
        cand = parse_hreflang_sitemap(sitemap_xml, sitemap_url or base_url)
        if cand:
            verdict = _confirm_en(cand, home_host, budget)
            if isinstance(verdict, dict):
                result.update(en_url=verdict["url"], source="hreflang_sitemap", en_home=verdict)
                return result

    # ── 4. probe /en/ и вариации ─────────────────────────────────────────────
    if probe:
        fin = urlparse(final_url)
        base = f"{fin.scheme}://{fin.netloc}"
        candidates = []
        # IKEA-паттерн: язык — ВТОРОЙ сегмент (/{cc}/{lang}/), EN на /nl/en/
        mcc = re.match(r"^/([a-z]{2})/[a-z]{2}(/|$)", fin.path, re.IGNORECASE)
        if mcc:
            candidates.append(f"{base}/{mcc.group(1)}/en/")
        candidates += [f"{base}/en/", f"{base}/en-us/", f"{base}/en-gb/"]
        # POST-переключатели (Shopify/OpenCart) не эмулируем, но их статические
        # locale-коды в HTML — сигнал «EN существует» → целевой probe
        for code in re.findall(r'(?:data-name|locale_code|data-locale)\s*=\s*["\'](en(?:-[a-z]{2})?)["\']',
                               html, re.IGNORECASE):
            extra = f"{base}/{code.lower()}/"
            if extra not in candidates:
                candidates.append(extra)
        for cand in candidates:
            verdict = _confirm_en(cand, home_host, budget)
            if isinstance(verdict, dict):
                result.update(en_url=verdict["url"], source="probe", en_home=verdict)
                return result

    # ── 5. GET-переключатели с redirect (concrete5: 302 → /en) ──────────────
    for endpoint in switchers[:MAX_SWITCHER_FOLLOWS]:
        verdict = _confirm_en(endpoint, home_host, budget)
        if isinstance(verdict, dict):
            result.update(en_url=verdict["url"], source="switcher", en_home=verdict)
            return result

    log_debug(f"discover_language_versions: EN-версия не найдена "
              f"(default={result['default_lang']}, external_en={result['external_en']})")
    return result


# ─── Фильтр EN-поддерева ─────────────────────────────────────────────────────

def keep_lang_subtree(urls: list, lang_root: str):
    """Оставляет только URL внутри дерева lang_root (для EN-пула после switch).
    Хост сравнивается с точностью до www., путь — root или root/... ."""
    p = urlparse(lang_root)
    root_path = p.path.rstrip("/")
    root_reg = _registrable_domain(p.netloc)
    kept, removed = [], 0
    for u in urls:
        q = urlparse(u)
        qpath = q.path.rstrip("/")
        if (_registrable_domain(q.netloc) == root_reg
                and (qpath == root_path or q.path.startswith(root_path + "/"))):
            kept.append(u)
        else:
            removed += 1
    return kept, removed


# ─── CLI для ручной проверки ─────────────────────────────────────────────────

if __name__ == "__main__":
    from utils import setup_console, normalize_url
    setup_console()  # UTF-8 до первого вывода (правило cp1252)
    if len(sys.argv) < 2:
        print("Usage: python language_detector.py <domain> [<domain2> ...]")
        sys.exit(1)

    import requests
    for domain in sys.argv[1:]:
        base = normalize_url(domain)
        try:
            r = requests.get(base, headers=HEADERS, timeout=_TIMEOUT, allow_redirects=True)
        except Exception as e:
            log_warn(f"{domain}: главная не скачалась ({type(e).__name__})")
            continue
        res = discover_language_versions(r.text, dict(r.headers), base, final_url=r.url)
        log_info(f"{domain:<28} default={res['default_lang']:<8} "
                 f"en_url={res['en_url'] or '—':<42} source={res['source'] or '—':<15} "
                 f"external_en={res['external_en'] or '—'}")
