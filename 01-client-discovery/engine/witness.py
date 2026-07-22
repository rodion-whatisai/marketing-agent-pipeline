# -*- coding: utf-8 -*-
"""
witness.py — «свидетель» испытательного стенда: тупой регистратор трафика.

НЕ содержит ни строки парсерного кода сканера (свои паттерны, свои consent-клики,
своё чтение POST-тел) — этим и ценен: его записи — независимые улики для эталонов
golden/ (см. TESTBED-PLAN.md, «Правило гейта»). Улики показываются Rodion'у,
правдой факт становится только после его «да».

Происхождение: промоут diag_meta_post.py (диагностика POST-слепоты 2026-07-13,
scans/_diag_meta_post_2026-07-13/README.md).

Два режима:
  --pages    обход страниц ЗАМОРОЖЕННОГО golden/<domain>/step1.json: на страницу —
             финальный URL + HTTP-статус (правда о редиректах/404), все запросы к
             пиксель-хостам (метод + query-событие + события из POST-тела + fixture),
             сырой список видимых кликабельных текстов (БЕЗ SKIP-фильтров сканера),
             сторонние хосты, скриншот.
             Коммитимый вывод: golden/<domain>/witness_<date>.json (тела → snippet+sha1);
             сырьё (скриншоты, полные тела): scans/_witness_<date>/<domain>/ (gitignored).
  --journey  e-com путь product → add-to-cart → cart → checkout (исходный diag-код,
             для Shopify). Вывод дополнительно копируется в
             golden/<domain>/witness_journey_<date>.json.

Ничего не вводит, формы не сабмитит. Checkout только открывается (InitiateCheckout),
покупка не совершается. Классификация платформ — eval_lib.EVIDENCE_PROBES (тупые
подстроки хостов, общая независимая таблица стенда). Из движка импортируется только
utils.setup_console + eval_lib — из scanners/ НИЧЕГО (независимость от парсера).

usage: python witness.py <domain> --pages [--headed]
       python witness.py <domain> --journey [--headed] [--product-url URL]
"""
import json, re, sys, time, argparse, hashlib, subprocess, datetime
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright

from utils import setup_console
setup_console()  # UTF-8 до первого вывода — cp1252-крэш на эмодзи/кириллице

import eval_lib as ev

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# Классификация платформ — общая независимая таблица стенда (тупые подстроки
# хостов, не парсер сканера). Слепое пятно общего списка закрывает канал B
# (руки Rodion'а) — см. TESTBED-PLAN.md «Риски».
PIXEL_PATTERNS = ev.EVIDENCE_PROBES

QUERY_EVENT_PARAMS = ["ev", "en", "event", "ea"]

CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler", ".cky-btn-accept", "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button[data-testid*='accept']", "button#accept-cookies",
]
CONSENT_TEXTS = ["accept all", "accept", "alle akzeptieren", "akzeptieren", "tout accepter",
                 "j'accepte", "agree", "allow all", "got it", "i understand", "ok",
                 # матч точный (^…$), поэтому "ok" НЕ ловит "Okay" — нужна своя запись.
                 # Tested: 2026-07-22 on plurio.ai — чип "We use cookies… [Okay]"
                 "okay"]

ATC_SELECTORS = [
    "button[name='add']", "[name='add']", "form[action*='/cart/add'] button[type='submit']",
    "form[action*='/cart/add'] [type='submit']",
]
ATC_TEXTS = ["add to cart", "add to bag", "in den warenkorb", "ajouter au panier", "add"]

CHECKOUT_SELECTORS = ["button[name='checkout']", "[name='checkout']", "a[href*='/checkout']",
                      "button[data-testid*='checkout']"]
CHECKOUT_TEXTS = ["check out", "checkout", "zur kasse", "kasse", "passer la commande", "commander"]


def find_product_url(domain: str) -> tuple:
    """Стандартный Shopify /products.json → первый товар с available-вариантом.
    Возвращает (url_with_variant, source)."""
    for host in (f"https://www.{domain}", f"https://{domain}"):
        try:
            r = requests.get(f"{host}/products.json?limit=20", timeout=15,
                             headers={"User-Agent": UA})
            if r.status_code != 200 or "products" not in r.text[:200]:
                continue
            for p in r.json().get("products", []):
                for v in p.get("variants", []):
                    if v.get("available"):
                        return (f"{host}/products/{p['handle']}?variant={v['id']}",
                                "products.json")
        except Exception:
            continue
    return (None, "not_found")


def classify(url: str) -> str:
    for plat, pats in PIXEL_PATTERNS.items():
        if any(p in url for p in pats):
            return plat
    return ""


def extract_query_event(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    for p in QUERY_EVENT_PARAMS:
        if p in q and q[p]:
            return q[p][0]
    return ""


def extract_body_events(body: str) -> list:
    """Достаёт имена событий из POST-тела: multipart form-data (Meta),
    form-encoded ev=/en=/event=, ИЛИ JSON "event": "..." (TikTok)."""
    if not body:
        return []
    out = []
    # multipart/form-data (Meta): блок name="ev" → значение на следующей непустой строке
    if "form-data" in body[:2000]:
        for p in QUERY_EVENT_PARAMS:
            out += re.findall(
                rf'name="{p}"\s*\r?\n\r?\n([^\r\n]{{1,60}})', body)
    # form-encoded
    elif "=" in body and "{" not in body[:5]:
        try:
            q = parse_qs(body)
            for p in QUERY_EVENT_PARAMS:
                for v in q.get(p, []):
                    out.append(v)
        except Exception:
            pass
    # JSON (в т.ч. batch) — regex надёжнее полного парса на обрезках
    out += re.findall(r'"event"\s*:\s*"([^"]{1,60})"', body)
    # dedup, порядок сохраняем
    seen, res = set(), []
    for e in out:
        if e not in seen:
            seen.add(e); res.append(e)
    return res


def try_click(page, selectors, texts, timeout=2500) -> str:
    """Пробует селекторы, потом кнопки по тексту. Возвращает описание кликнутого или ''."""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=timeout)
                return f"selector:{sel}"
        except Exception:
            continue
    for t in texts:
        try:
            loc = page.locator(f"button:visible, [role='button']:visible, a:visible").filter(
                has_text=re.compile(rf"^\s*{re.escape(t)}\s*$", re.I)).first
            if loc.count():
                loc.click(timeout=timeout)
                return f"text:{t}"
        except Exception:
            continue
    # <input type="button" value="Okay"> (Framer cookie-чип): текста внутри нет —
    # has_text его не видит, CSS [role='button'] тоже (роль неявная). Ловим через
    # accessibility-роль: она покрывает и input[type=button/submit] по value.
    # Tested: 2026-07-22 on plurio.ai/connectors/cart
    for t in texts:
        try:
            loc = page.get_by_role("button", name=re.compile(rf"^\s*{re.escape(t)}\s*$", re.I)).first
            if loc.count():
                loc.click(timeout=timeout)
                return f"role:{t}"
        except Exception:
            continue
    return ""


def run(domain: str, headed: bool, product_url: str, outdir: Path) -> dict:
    outdir.mkdir(parents=True, exist_ok=True)
    fixtures = outdir / "fixtures"
    fixtures.mkdir(exist_ok=True)

    if not product_url:
        product_url, src = find_product_url(domain)
        print(f"[{domain}] product discovery ({src}): {product_url}")
    if not product_url:
        return {"domain": domain, "error": "no_product_url"}

    captured = []          # все pixel-запросы
    phase = {"now": "load"}

    def on_request(request):
        plat = classify(request.url)
        if not plat:
            return
        body = None
        try:
            body = request.post_data
        except Exception:
            pass
        rec = {
            "ts": round(time.time() - t0, 1),
            "phase": phase["now"],
            "platform": plat,
            "method": request.method,
            "url": request.url[:180],
            "query_event": extract_query_event(request.url),
            "body_events": extract_body_events(body or ""),
            "has_body": bool(body),
        }
        if body and plat in ("Meta", "TikTok") and request.method == "POST":
            h = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:10]
            fp = fixtures / f"{domain}_{plat}_{phase['now']}_{h}.txt"
            fp.write_text(body, encoding="utf-8")
            rec["body_file"] = fp.name
        captured.append(rec)

    result = {"domain": domain, "product_url": product_url, "headed": headed, "steps": {}}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        ctx.on("request", on_request)
        t0 = time.time()

        # ── PHASE 1: product load ──
        phase["now"] = "load"
        try:
            page.goto(product_url, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            result["steps"]["load"] = f"goto failed: {e}"
            browser.close()
            result["captured"] = captured
            return result
        page.wait_for_timeout(4000)
        clicked_consent = try_click(page, CONSENT_SELECTORS, CONSENT_TEXTS, timeout=1500)
        result["steps"]["consent"] = clicked_consent or "no banner clicked"
        page.wait_for_timeout(8000)
        result["steps"]["load"] = "ok"

        # ── PHASE 2: add to cart ──
        phase["now"] = "atc"
        clicked = try_click(page, ATC_SELECTORS, ATC_TEXTS, timeout=6000)
        result["steps"]["atc_click"] = clicked or "NOT CLICKED"
        page.wait_for_timeout(10000)
        result["steps"]["url_after_atc"] = page.url[:120]

        # ── PHASE 3: cart ──
        phase["now"] = "cart"
        base = f"{urlparse(product_url).scheme}://{urlparse(product_url).netloc}"
        if "/cart" not in page.url:
            try:
                page.goto(base + "/cart", timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                result["steps"]["cart"] = f"goto failed: {e}"
        page.wait_for_timeout(5000)
        result["steps"]["cart"] = page.url[:120]

        # ── PHASE 4: checkout (только открытие, ничего не вводим) ──
        phase["now"] = "checkout"
        clicked_co = try_click(page, CHECKOUT_SELECTORS, CHECKOUT_TEXTS, timeout=6000)
        result["steps"]["checkout_click"] = clicked_co or "NOT CLICKED"
        for attempt in range(2):
            try:
                page.wait_for_url(re.compile(r"/(checkouts?|checkout)/"), timeout=30000)
                break
            except Exception:
                if attempt == 0 and "/cart" in page.url:  # клик не сработал — повтор
                    try_click(page, CHECKOUT_SELECTORS, CHECKOUT_TEXTS, timeout=6000)
        page.wait_for_timeout(10000)
        result["steps"]["url_after_checkout"] = page.url[:120]

        browser.close()

    result["captured"] = captured
    mode = "headed" if headed else "headless"
    out = outdir / f"{domain}_{mode}.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")

    # ── человекочитаемая сводка ──
    print(f"\n===== {domain} ({mode}) =====")
    for st, v in result["steps"].items():
        print(f"  step {st}: {v}")
    print(f"  {'phase':9} {'platform':10} {'meth':5} {'query_event':22} body_events")
    for r in captured:
        if "connect.facebook.net" in r["url"]:   # SDK-загрузка — не событие, шум в сводке
            continue
        print(f"  {r['phase']:9} {r['platform']:10} {r['method']:5} "
              f"{(r['query_event'] or '-'):22} {','.join(r['body_events']) or '-'}")
    return result


# ─── Pages mode — обход страниц замороженного step1 ──────────────────────────

_CLICKABLES_JS = """
() => {
  const els = Array.from(document.querySelectorAll(
    "button, [role='button'], a, input[type='submit'], input[type='button']"));
  const texts = [];
  for (const el of els) {
    if (!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)) continue;
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .trim().replace(/\\s+/g, ' ');
    if (t && t.length <= 80 && !texts.includes(t)) texts.push(t);
    if (texts.length >= 80) break;
  }
  return texts;
}
"""

BODY_SNIPPET_CAP = 300   # в коммитимом JSON; полное тело — в fixture-файле (gitignored)


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, cwd=ev.ENGINE_DIR)
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _safe_name(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "_", path.strip("/") or "home")[:60]


def run_pages(domain: str, headed: bool = False) -> dict:
    """Свидетель по страницам замороженного step1. Пишет golden/<domain>/witness_<date>.json."""
    date = datetime.date.today().isoformat()
    s1_path = ev.frozen_step1_path(domain)
    if not s1_path.exists():
        print(f"нет замороженного step1: {s1_path}")
        sys.exit(1)
    step1 = ev.load_json(s1_path)
    pages_list = [p for p in step1.get("to_scan", []) if p.get("priority", 5) <= 2]
    if not pages_list:
        print("в замороженном step1 нет страниц priority<=2")
        sys.exit(1)

    raw_dir = ev.ENGINE_DIR / "scans" / f"_witness_{date}" / domain
    fixtures = raw_dir / "fixtures"
    fixtures.mkdir(parents=True, exist_ok=True)
    base_token = domain[4:] if domain.startswith("www.") else domain   # для отсева своих хостов

    result = {
        "schema_version": 1, "domain": domain, "mode": "pages", "date": date,
        "witness_commit": _git_commit(), "headless": not headed,
        "consent": {"clicked": None, "on_page": None},
        "pages": [],
    }

    current = {"rec": None}

    def on_request(request):
        rec = current["rec"]
        if rec is None:
            return
        url = request.url
        host = urlparse(url).netloc
        if host:
            rec["_hosts"].add(host)
        plat = classify(url)
        if not plat:
            return
        body = None
        try:
            body = request.post_data
        except Exception:
            pass
        entry = {
            "platform": plat, "method": request.method, "url": url[:500],
            "query_event": extract_query_event(url),
            "body_events": extract_body_events(body or ""),
        }
        if body:
            h = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:10]
            fp = fixtures / f"{domain}_{plat}_{_safe_name(rec['path'])}_{h}.txt"
            fp.write_text(body, encoding="utf-8")
            entry["body_sha1"] = h
            entry["body_snippet"] = body[:BODY_SNIPPET_CAP]
            entry["body_file"] = str(fp.relative_to(ev.ENGINE_DIR)).replace("\\", "/")
        rec["pixel_requests"].append(entry)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        ctx.on("request", on_request)
        page = ctx.new_page()

        for i, item in enumerate(pages_list, 1):
            url, path = item["url"], item.get("path", "/")
            rec = {"path": path, "requested_url": url, "http_status": None,
                   "final_url": None, "redirected": None,
                   "pixel_requests": [], "clickables": [], "third_party_hosts": [],
                   "screenshot": None, "_hosts": set()}
            current["rec"] = rec
            print(f"[{i}/{len(pages_list)}] {path}")
            try:
                resp = page.goto(url, timeout=45000, wait_until="domcontentloaded")
                rec["http_status"] = resp.status if resp else None
            except Exception as e:
                rec["error"] = f"goto: {e}"[:200]
                rec.pop("_hosts")
                result["pages"].append(rec)
                continue
            page.wait_for_timeout(4000)
            if i == 1:
                clicked = try_click(page, CONSENT_SELECTORS, CONSENT_TEXTS, timeout=1500)
                if not clicked:
                    # Framer-чип рендерится ~на 5-й секунде — одна поздняя попытка.
                    # Tested: 2026-07-22 on plurio.ai (первый заход в 4с промахивался)
                    page.wait_for_timeout(3500)
                    clicked = try_click(page, CONSENT_SELECTORS, CONSENT_TEXTS, timeout=1500)
                result["consent"] = {"clicked": clicked or None, "on_page": path}
                print(f"    consent: {clicked or 'баннер не кликнут'}")
                page.wait_for_timeout(3000)
            page.wait_for_timeout(8000)

            rec["final_url"] = page.url[:300]
            req, fin = urlparse(url), urlparse(page.url)
            rec["redirected"] = (req.netloc.replace("www.", "") != fin.netloc.replace("www.", "")
                                 or (req.path.rstrip("/") or "/") != (fin.path.rstrip("/") or "/"))
            try:
                rec["clickables"] = page.evaluate(_CLICKABLES_JS)
            except Exception:
                pass
            shot = raw_dir / f"{i:02d}_{_safe_name(path)}.png"
            try:
                page.screenshot(path=str(shot))
                rec["screenshot"] = str(shot.relative_to(ev.ENGINE_DIR)).replace("\\", "/")
            except Exception:
                pass
            hosts = rec.pop("_hosts")
            rec["third_party_hosts"] = sorted(h for h in hosts if base_token not in h)[:60]
            current["rec"] = None
            plats = sorted({r["platform"] for r in rec["pixel_requests"]})
            print(f"    status={rec['http_status']} redirected={rec['redirected']} "
                  f"платформы={','.join(plats) or '—'} кликабельных={len(rec['clickables'])}")
            result["pages"].append(rec)

        browser.close()

    out = ev.GOLDEN_DIR / domain / f"witness_{date}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nсвидетель записан: {out}")
    print(f"сырьё (скриншоты, полные тела): {raw_dir}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Свидетель стенда: --pages или --journey (см. докстринг)")
    ap.add_argument("domain")
    ap.add_argument("--pages", action="store_true", help="обход страниц замороженного step1")
    ap.add_argument("--journey", action="store_true", help="e-com путь product→ATC→cart→checkout (Shopify)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--product-url", default=None, help="(journey) конкретная продуктовая страница")
    ap.add_argument("--outdir", default=None, help="(journey) куда класть сырьё")
    a = ap.parse_args()

    if a.pages == a.journey:   # оба или ни одного
        ap.error("выбери режим: --pages ИЛИ --journey")

    if a.pages:
        run_pages(a.domain, headed=a.headed)
    else:
        date = datetime.date.today().isoformat()
        outdir = Path(a.outdir) if a.outdir else ev.ENGINE_DIR / "scans" / f"_witness_{date}" / a.domain
        res = run(a.domain, a.headed, a.product_url, outdir)
        # компактная копия в golden — journey-записи уже маленькие (тела в fixtures)
        if res and "captured" in res:
            gout = ev.GOLDEN_DIR / a.domain / f"witness_journey_{date}.json"
            gout.parent.mkdir(parents=True, exist_ok=True)
            gout.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"journey-улики скопированы: {gout}")
