# -*- coding: utf-8 -*-
"""
witness.py — «свидетель» испытательного стенда: тупой регистратор трафика.

НЕ содержит ни строки парсерного кода сканера (свои паттерны, свои consent-клики,
своё чтение POST-тел) — этим и ценен: его записи — независимые улики для эталонов
golden/ (см. TESTBED-PLAN.md, «Правило гейта»). Улики показываются Rodion'у,
правдой факт становится только после его «да».

Происхождение: промоут diag_meta_post.py (диагностика POST-слепоты 2026-07-13,
scans/_diag_meta_post_2026-07-13/README.md) — скопирован как есть из temp-scratchpad
чужой сессии. День 3 плана добавит режим --pages (обход страниц замороженного step1)
к текущему journey-режиму (product → add-to-cart → cart → checkout).

Ничего не вводит, формы не сабмитит. Checkout только открывается (InitiateCheckout),
покупка не совершается.

usage: python witness.py <domain> [--headed] [--product-url URL] [--outdir DIR]
"""
import json, re, sys, time, argparse, hashlib
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

PIXEL_PATTERNS = {
    "Meta":       ["facebook.com/tr"],
    "Meta-SDK":   ["connect.facebook.net"],
    "TikTok":     ["analytics.tiktok.com"],
    "GA":         ["analytics.google.com/g/collect", "google-analytics.com/g/collect",
                   "google-analytics.com/collect"],
    "GoogleAds":  ["/ccm/collect", "googleadservices.com/pagead/conversion",
                   "/pagead/1p-conversion"],
    "Snap":       ["tr.snapchat.com"],
    "Pinterest":  ["ct.pinterest.com"],
}

QUERY_EVENT_PARAMS = ["ev", "en", "event", "ea"]

CONSENT_SELECTORS = [
    "#onetrust-accept-btn-handler", ".cky-btn-accept", "#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll",
    "button[data-testid*='accept']", "button#accept-cookies",
]
CONSENT_TEXTS = ["accept all", "accept", "alle akzeptieren", "akzeptieren", "tout accepter",
                 "j'accepte", "agree", "allow all", "got it", "i understand", "ok"]

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
        if r["platform"] in ("Meta-SDK",):
            continue
        print(f"  {r['phase']:9} {r['platform']:10} {r['method']:5} "
              f"{(r['query_event'] or '-'):22} {','.join(r['body_events']) or '-'}")
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("domain")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--product-url", default=None)
    ap.add_argument("--outdir", default=r"C:\Users\user\SiteScannerv4\01-client-discovery\engine\scans\_diag_meta_post_2026-07-13")
    a = ap.parse_args()
    run(a.domain, a.headed, a.product_url, Path(a.outdir))
