"""
TNC Google Ads Transparency Center — Domain Resolver
====================================================
Открывает прямой URL `?region=FR&domain=<X>` — это страница TC где
Google показывает ВСЕХ advertisers с ads, ведущими на этот домен.

Возвращает: всех advertisers + всех creatives для домена.

Запуск:
    python google_ads_domain.py miessler-automotive.com
    python google_ads_domain.py --file domains_fr_competitors.txt
    python google_ads_domain.py points.fr --headed --verbose
"""

import sys
import re
import json
import argparse
from pathlib import Path
from urllib.parse import urlparse, quote

from utils import HEADERS, get_scan_dir, SCANS_DIR


TC_BASE = "https://adstransparency.google.com"
DEFAULT_REGION = "FR"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _normalize_domain(domain: str) -> str:
    d = domain.lower().strip()
    if "://" in d:
        d = urlparse(d).netloc
    d = d.replace("www.", "")
    return d


def _strip_subdomain(domain: str) -> str | None:
    """fr.maxpeedingrods.com → maxpeedingrods.com.
    Возвращает None если subdomain нет (только TLD-уровень)."""
    parts = domain.split('.')
    if len(parts) <= 2:
        return None
    # Если первая часть — обычный subdomain (fr, en, de, shop, blog, app, m...)
    SUBDOMAIN_PREFIXES = {'fr', 'en', 'de', 'es', 'it', 'nl', 'pt', 'pl',
                          'shop', 'blog', 'app', 'm', 'mobile', 'store', 'www'}
    if parts[0] in SUBDOMAIN_PREFIXES:
        return '.'.join(parts[1:])
    # Иначе тоже стрипим, но осторожно (могут быть compound TLD типа .co.uk)
    return '.'.join(parts[1:])


def _build_url(domain: str, region: str) -> str:
    return f"{TC_BASE}/?region={region}&domain={quote(domain, safe='.-_~')}"


# ─── Page parser ─────────────────────────────────────────────────────────────

def _parse_total_ads(html: str) -> int | None:
    """
    Понимает разные форматы:
        '12 ads' → 12
        '~600 ads' → 600
        '~2K ads' → 2000
        '~1.5K ads' → 1500
        '~3M ads' → 3_000_000
    """
    # K/M suffix variant
    m = re.search(r'(?:~\s*)?([\d\.,]+)\s*([KM])\s+ads\b', html)
    if m:
        num = float(m.group(1).replace(',', ''))
        mult = 1_000 if m.group(2).upper() == 'K' else 1_000_000
        return int(num * mult)
    # Plain integer
    m = re.search(r'(?:~\s*)?(\d[\d,]*)\s+ads\b', html)
    if m:
        return int(m.group(1).replace(',', ''))
    return None


def _parse_advertisers(html: str) -> list[dict]:
    """Парсит advertiser-info-card блоки (если есть на странице ?domain=X)."""
    advertisers = []
    # Каждый advertiser — block с advertiser-name + Verified status
    # Ищем по структуре карточки.
    pattern = re.compile(
        r'<div[^>]*class="[^"]*advertiser-name[^"]*"[^>]*>([^<]+?)</div>',
        re.DOTALL,
    )
    seen = set()
    for m in pattern.finditer(html):
        name = _clean_text(m.group(1))
        if name and name not in seen:
            seen.add(name)
            advertisers.append({"name": name})
    return advertisers


def _parse_creatives(html: str, region: str) -> list[dict]:
    """Парсит creative cards. Возвращает [{creative_id, advertiser_id, full_url}]."""
    creatives = []
    seen = set()
    # href="/advertiser/AR.../creative/CR...?region=FR"
    pattern = re.compile(
        r'href="(/advertiser/(AR\d+)/creative/(CR\d+)\?region=' + re.escape(region) + r')"'
    )
    for m in pattern.finditer(html):
        path = m.group(1)
        ar = m.group(2)
        cr = m.group(3)
        if cr in seen:
            continue
        seen.add(cr)
        creatives.append({
            "creative_id": cr,
            "advertiser_id": ar,
            "creative_page_url": TC_BASE + path,
        })
    return creatives


def _parse_advertiser_meta_from_card(html: str) -> list[dict]:
    """Достаёт meta для advertiser cards в ?domain=X странице.
    На разных view может быть разная структура — собираем как можем."""
    cards = []
    # Card pattern: <X advertiser block> + <name> + 'Verified'
    # Берём блоки с advertiser-name + ближайший Verified маркер.
    name_pattern = re.compile(
        r'<div[^>]*class="[^"]*advertiser-name[^"]*"[^>]*>([^<]+?)</div>',
        re.DOTALL,
    )
    seen_names = set()
    for m in name_pattern.finditer(html):
        name = _clean_text(m.group(1))
        if not name or name in seen_names:
            continue
        seen_names.add(name)
        # Look for Verified within ~500 chars after this name
        tail = html[m.end():m.end() + 500]
        verified = "Verified" if re.search(r'\bVerified\b', tail, re.IGNORECASE) else None
        cards.append({"name": name, "verification_status": verified})
    return cards


def _clean_text(s: str) -> str:
    import html as _html
    s = _html.unescape(s)
    s = re.sub(r'\s+', ' ', s).strip(' .,:-')
    return s


# ─── Pagination handling — "See all ads" / scroll ────────────────────────────

def _try_expand_all_ads(page, region: str, verbose: bool = False,
                          max_iters: int = 500, stable_threshold: int = 5) -> dict:
    """
    1. Жмём 'See all ads' если есть.
    2. Скроллим, ИНКРЕМЕНТАЛЬНО собирая creative_ids + advertiser names в sets.
       Google использует virtualized DOM (top cards выкидывают при scroll),
       поэтому финальный page.content() показывает только viewport window.
       Собираем по мере прохождения.
    Возвращает {clicked_button, scroll_iterations, seen_creatives, seen_advertisers}.
    """
    before = _count_ad_cards(page)
    if verbose:
        print(f"    initial creatives in DOM: {before}")

    button_labels = [
        'See all ads', 'see all ads',
        'View all ads', 'view all ads',
        'See more results', 'See more',
        'Voir toutes les annonces',
        'Alle Anzeigen anzeigen',
    ]
    clicked_button = None
    url_before = page.url
    for label in button_labels:
        try:
            btn = page.locator(f'text="{label}"').first
            if btn.count() > 0 and btn.is_visible():
                if verbose:
                    print(f"    found button: '{label}' — clicking")
                btn.click()
                page.wait_for_timeout(2500)
                clicked_button = label
                break
        except Exception:
            continue
    if verbose and clicked_button:
        print(f"    after click: url_changed={url_before != page.url}, current_url={page.url}")

    # Incremental collection — virtualized scroll
    seen_creatives = {}      # cr_id → {advertiser_id, creative_page_url}
    seen_advertisers = {}    # name → {verification_status}

    href_re = re.compile(r'/advertiser/(AR\d+)/creative/(CR\d+)\?region=' + re.escape(region))

    def _scrape_current_dom():
        """Извлечь creative refs + advertiser names из текущего DOM."""
        # Creatives
        try:
            anchors = page.locator('a[href*="/advertiser/AR"][href*="/creative/CR"]').all()
            for a in anchors:
                try:
                    href = a.get_attribute('href') or ''
                    m = href_re.search(href)
                    if m:
                        ar, cr = m.group(1), m.group(2)
                        if cr not in seen_creatives:
                            seen_creatives[cr] = {
                                "advertiser_id": ar,
                                "creative_page_url": TC_BASE + m.group(0).rstrip(),
                            }
                except Exception:
                    continue
        except Exception:
            pass
        # Advertiser cards (current visible)
        try:
            html = page.content()
            for nm in _parse_advertiser_meta_from_card(html):
                key = nm["name"]
                if key and key not in seen_advertisers:
                    seen_advertisers[key] = nm
        except Exception:
            pass

    _scrape_current_dom()
    if verbose:
        print(f"    after initial scrape: creatives={len(seen_creatives)}, advertisers={len(seen_advertisers)}")

    prev_count = -1
    stable = 0
    iters = 0
    MAX_ITERS = max_iters
    STABLE_THRESHOLD = stable_threshold
    while stable < STABLE_THRESHOLD and iters < MAX_ITERS:
        # Strategy: scroll the LAST visible creative card into view.
        # Это надежно триггерит IntersectionObserver/lazy-loading
        # для virtualized scroll'а.
        scrolled = False
        try:
            cards = page.locator('a[href*="/advertiser/AR"][href*="/creative/CR"]').all()
            if cards:
                try:
                    cards[-1].scroll_into_view_if_needed(timeout=3000)
                    scrolled = True
                except Exception:
                    pass
        except Exception:
            pass

        # Fallback — обычный scrollTo если нет cards в viewport
        if not scrolled:
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass

        # Дополнительно — small wheel push чтобы trigger 'scroll' event
        try:
            page.mouse.wheel(0, 1000)
        except Exception:
            pass

        page.wait_for_timeout(2500)

        _scrape_current_dom()
        cur_count = len(seen_creatives)
        if cur_count == prev_count:
            stable += 1
        else:
            stable = 0
        prev_count = cur_count
        iters += 1
        if verbose and (iters % 5 == 0 or stable == 0):
            body_h = 0
            try:
                body_h = page.evaluate("document.body.scrollHeight")
            except Exception:
                pass
            print(f"    iter={iters}, unique={cur_count}, adv={len(seen_advertisers)}, stable={stable}, body_h={body_h}")

    return {
        "clicked_button": clicked_button,
        "scroll_iterations": iters,
        "seen_creatives": seen_creatives,
        "seen_advertisers": seen_advertisers,
    }


def _count_ad_cards(page) -> int:
    try:
        return page.locator('a[href*="/advertiser/AR"][href*="/creative/CR"]').count()
    except Exception:
        return 0


# ─── Main fetch ──────────────────────────────────────────────────────────────

def fetch_domain_page(domain: str, region: str = DEFAULT_REGION,
                      headed: bool = False, verbose: bool = False,
                      allow_subdomain_strip: bool = True,
                      max_iters: int = 500, stable_threshold: int = 5) -> dict:
    """
    Открывает ?domain=<X> и парсит advertisers + creatives.
    Если пусто — пробует stripped subdomain.
    """
    from playwright.sync_api import sync_playwright

    domain_clean = _normalize_domain(domain)

    result = {
        "domain": domain,
        "domain_clean": domain_clean,
        "region": region,
        "url_used": None,
        "total_ads_estimate": None,
        "advertisers": [],
        "creatives": [],
        "expansion": None,
        "flags": [],
        "error": None,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            ctx = browser.new_context(user_agent=HEADERS["User-Agent"])
            page = ctx.new_page()

            # First attempt: full domain
            tried = [domain_clean]
            success = _attempt_domain(page, domain_clean, region, result, verbose=verbose,
                                      max_iters=max_iters, stable_threshold=stable_threshold)

            # Fallback: strip subdomain
            if not success and allow_subdomain_strip:
                stripped = _strip_subdomain(domain_clean)
                if stripped and stripped not in tried:
                    if verbose:
                        print(f"    empty for '{domain_clean}' — trying stripped '{stripped}'")
                    result["flags"].append("subdomain_stripped")
                    tried.append(stripped)
                    _attempt_domain(page, stripped, region, result, verbose=verbose,
                                    max_iters=max_iters, stable_threshold=stable_threshold)

            browser.close()
            return result

    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        return result


def _attempt_domain(page, domain: str, region: str, result: dict, verbose: bool = False,
                     max_iters: int = 500, stable_threshold: int = 5) -> bool:
    """One attempt for given domain. Returns True if non-empty result was found."""
    url = _build_url(domain, region)
    if verbose:
        print(f"  [domain] {domain}  url={url}")
    page.goto(url, timeout=30_000, wait_until="domcontentloaded")
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass
    page.wait_for_timeout(1500)

    # Quick check: is there content?
    initial_html = page.content()
    total = _parse_total_ads(initial_html)
    initial_creatives = _count_ad_cards(page)

    if verbose:
        print(f"    total_ads_estimate={total}, initial_creatives={initial_creatives}")

    if (total is None or total == 0) and initial_creatives == 0:
        # Empty page — don't update result
        return False

    # Expand all ads (button + virtualized scroll, incremental collection)
    expansion = _try_expand_all_ads(page, region=region, verbose=verbose,
                                     max_iters=max_iters, stable_threshold=stable_threshold)

    # Build results from incremental sets (virtualized DOM = можем не быть в финальном HTML)
    seen_creatives = expansion.pop("seen_creatives", {})
    seen_advertisers = expansion.pop("seen_advertisers", {})

    creatives = [
        {"creative_id": cr, **info} for cr, info in seen_creatives.items()
    ]
    advertisers = list(seen_advertisers.values())

    result["url_used"] = url
    result["domain_clean"] = domain
    result["total_ads_estimate"] = total
    result["advertisers"] = advertisers
    result["creatives"] = creatives
    result["expansion"] = expansion

    if total and len(creatives) < total * 0.5:
        result["flags"].append(f"only_loaded_{len(creatives)}_of_~{total}")

    return True


# ─── CLI ─────────────────────────────────────────────────────────────────────

def _print_result(r: dict):
    if r.get("error"):
        print(f"❌ {r['domain']:30s}  ERROR: {r['error']}")
        return
    if not r.get("creatives") and not r.get("advertisers"):
        print(f"⚪ {r['domain']:30s}  empty (no advertisers/creatives)")
        return
    n_adv = len(r["advertisers"])
    n_cr = len(r["creatives"])
    total = r.get("total_ads_estimate")
    flags = r.get("flags") or []
    flags_str = (" " + ",".join(flags)) if flags else ""
    print(f"✅ {r['domain']:30s}  advertisers={n_adv}  creatives={n_cr}/{total or '?'}{flags_str}")
    for a in r["advertisers"][:5]:
        ver = a.get("verification_status") or "?"
        print(f"     • {a['name']}  [{ver}]")


def _save_to_scan(r: dict):
    if not r.get("domain"):
        return
    try:
        path = get_scan_dir(r["domain"]) / "google_ads_domain.json"
        path.write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?")
    ap.add_argument("--file")
    ap.add_argument("--region", default=DEFAULT_REGION)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--max-iters", type=int, default=500,
                    help="Max scroll iterations during virtualized expand (default 500)")
    ap.add_argument("--stable-threshold", type=int, default=5,
                    help="Stop after N consecutive iters without new creatives (default 5)")
    args = ap.parse_args()

    if args.file:
        domains = []
        for line in Path(args.file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                domains.append(line)
    elif args.target:
        domains = [args.target]
    else:
        ap.error("Provide a domain or --file")

    summary = []
    for d in domains:
        r = fetch_domain_page(d, region=args.region, headed=args.headed, verbose=args.verbose,
                                max_iters=args.max_iters, stable_threshold=args.stable_threshold)
        _print_result(r)
        summary.append(r)
        if not args.no_save:
            _save_to_scan(r)

    if args.file and not args.no_save:
        SCANS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = SCANS_DIR / f"_domain_summary_{args.region.lower()}.json"
        out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        ok = [r for r in summary if r.get("creatives") or r.get("advertisers")]
        print(f"\n  with_data: {len(ok)}/{len(summary)}")
        print(f"  summary saved: {out_path}")


if __name__ == "__main__":
    main()
