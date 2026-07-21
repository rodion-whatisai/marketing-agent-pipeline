"""
Base Scanner — общая логика для всех платформ.
Pixel detection, CTA JS, external services, статус страницы.
"""

import re
from urllib.parse import urlparse, parse_qs

from page_classifier import classify_page_content
from log import log_debug, log_fire


# ─── Общий детектор кнопок/CTA (один источник истины для сканера и кликера) ────
# Сканер находит кнопки ОДИН раз, помечает их data-tnc-btn, отдаёт список; кликер
# берёт этот же список. Раньше детекторов было два (узкий в сканере + широкий в
# кликере) → «CTA: 0, но кликнули 5». Теперь оба зовут это.

MAX_BUTTONS = 12

# Слова CTA — для приоритезации кнопок (выше приоритет = раньше кликаем)
CTA_WORDS = (
    "book", "buy", "quote", "contact", "submit", "send", "apply", "get started",
    "add to cart", "add to bag", "add to basket", "request", "sign up", "subscribe",
    "order", "checkout", "demo", "enquir", "call", "get a", "reserve", "register",
    # FR
    "devis", "soumission", "contactez", "reserver", "commander", "ajouter",
)

# JS: дженерик-поиск кнопок. Стэмпит каждому выжившему кандидату data-tnc-btn="<i>"
# — стабильный хэндл для повторного локейта. Фильтрует nav/footer/cookie-шум и
# служебные тексты.
# Tested: 2026-07-08 nav-фильтр (role="navigation"/menubar/menu + голые названия
#         разделов + стрелки каруселей): tinytronics → только Toevoegen/Verlanglijst
#         (события целы); nissan.ie → VIEW OFFERS/DISCOVER MORE/EXPLORE THE RANGE;
#         thebodyshop → VIEW PRODUCT. Пункты меню в CTA не попадают.
_DISCOVER_BUTTONS_JS = """
() => {
    const NOISE_SELECTORS = [
        'header','nav','footer',
        '[role="navigation"]','[role="menubar"]','[role="menu"]',
        '[id*="header" i]','[class*="header" i]',
        '[id*="navbar" i]','[class*="navbar" i]',
        '[id*="footer" i]','[class*="footer" i]',
        '[id*="site-nav" i]','[class*="site-nav" i]',
        '[class*="menu-toggle" i]','[class*="mobile-menu" i]',
        '[class*="cookie" i]','[id*="cookie" i]',
        '[class*="consent" i]','[id*="gdpr" i]',
        '[class*="breadcrumb" i]','[class*="pagination" i]',
        '[class*="skip" i]','[class*="gm-style"]',
    ];
    const noiseNodes = new Set();
    NOISE_SELECTORS.forEach(sel => { try { document.querySelectorAll(sel).forEach(el => noiseNodes.add(el)); } catch(e){} });
    function isInNoise(el){ let n=el.parentElement; while(n && n!==document.body){ if(noiseNodes.has(n)) return true; n=n.parentElement; } return false; }

    const MAIN_SELECTORS = ['main','#main','#main-content','#content','#primary','.site-content','.site-main','#page','.page-content','article','.post','.page','.entry-content'];
    let mainZone=null;
    for(const sel of MAIN_SELECTORS){ try{ const el=document.querySelector(sel); if(el && el.offsetHeight>50){ mainZone=el; break; } }catch(e){} }

    const SKIP_TEXTS = new Set(['close','ok','okay','cancel','dismiss','skip','back','accept','accept all','reject all','decline','allow','deny','agree','i agree','got it','save preferences','necessary only','accept cookies','reject cookies','manage cookies','cookie settings','search','menu','home','privacy policy','terms of service','view all','see all','load more','show more','more','next','continue','no thanks','maybe later','share','follow','print','previous','pause','play','use my current location','use my location',
        // голые названия разделов = навигация, не CTA (страховка для меню вне nav-контейнеров)
        'products','orders','returns','account','my account','delivery','about','about us','news','blog',
        // стрелки каруселей — не CTA (tinytronics: 'Previous slide' давал 5s-таймаут на каждой странице)
        'previous slide','next slide','prev slide']);

    function getButtonText(el){
        const aria=(el.getAttribute('aria-label')||'').trim(); if(aria.length>1 && aria.length<80) return aria;
        const val=(el.getAttribute('value')||'').trim(); if(val.length>1 && val.length<80) return val;
        const raw=(el.innerText||el.textContent||'').trim();
        const lines=raw.split('\\n').map(l=>l.trim()).filter(l=>l.length>0);
        return lines[0]||'';
    }
    function isVisible(el){ const s=window.getComputedStyle(el); if(s.display==='none')return false; if(s.visibility==='hidden')return false; const r=el.getBoundingClientRect(); if(r.width===0 && r.height===0) return false; return true; }

    const BUTTON_SELECTORS = [
        'button','[role="button"]','input[type="submit"]','input[type="button"]',
        'a.button','a.btn','[class*="btn"]','[class*="cta"]',
        '.wpforms-submit','.wpcf7-submit','.elementor-button','.wp-block-button__link',
        'form button',
    ].join(', ');

    let candidates=[];
    try{ candidates = Array.from(document.querySelectorAll(BUTTON_SELECTORS)); }catch(e){ candidates=[]; }

    const results=[]; const seenTexts=new Set();
    candidates.forEach(el => {
        const text=getButtonText(el); const tl=text.toLowerCase();
        if(isInNoise(el)) return;
        if(!isVisible(el)) return;
        if(!text || text.length<2 || text.length>80) return;
        if(SKIP_TEXTS.has(tl)) return;
        if(/^[$£€¥₹]/.test(text.trim())) return;
        if(/^[0-9]+$/.test(text.trim())) return;
        if(seenTexts.has(tl)) return;
        seenTexts.add(tl);
        const idx = results.length;
        try { el.setAttribute('data-tnc-btn', String(idx)); } catch(e){}
        results.push({
            index: idx,
            text: text,
            tag: el.tagName.toLowerCase(),
            isFormSubmit: el.closest('form') !== null,
            inMain: mainZone ? mainZone.contains(el) : false,
        });
    });
    return results;
}
"""


def discover_buttons(page, debug: bool = False) -> list:
    """Все значимые кнопки страницы (с проставленным data-tnc-btn), отсортированы по
    приоритету и обрезаны до MAX_BUTTONS. Один детектор для сканера и кликера."""
    try:
        raw = page.evaluate(_DISCOVER_BUTTONS_JS)
    except Exception as e:
        log_debug(f"discover_buttons: evaluate error: {str(e)[:80]}")
        return []

    def prio(c):
        t = (c.get("text") or "").lower()
        if c.get("isFormSubmit"):
            return 0
        if any(w in t for w in CTA_WORDS):
            return 1
        if c.get("inMain"):
            return 2
        return 3

    raw.sort(key=prio)
    out = raw[:MAX_BUTTONS]
    log_debug(f"discover_buttons: {len(raw)} найдено → {len(out)} после cap {MAX_BUTTONS}")
    return out


# ─── Pixel rules ──────────────────────────────────────────────────────────────
# С 2026-07-13 источник знаний — ЕДИНЫЙ реестр platforms.py (шаг A рефакторинга,
# TESTBED-PLAN.md). Старые имена — производные view'ы; содержимое и порядок
# ключей эквивалентны прежним литералам (пины: test_platforms.py против
# замороженных копий). Комментарии-кейсы (Tested: ...) переехали в реестр.
import platforms as _platforms

PIXEL_RULES = _platforms.as_pixel_rules()
CONVERSION_EVENTS_TIER1 = _platforms.as_conversion_tier1()
CONVERSION_EVENTS_TIER2 = _platforms.as_conversion_tier2()
NOISE_EVENTS = _platforms.as_noise_events()

# ─── External services ────────────────────────────────────────────────────────

EXTERNAL_SERVICES = {
    "Calendly":          ["calendly.com"],
    "Acuity":            ["acuityscheduling.com", "squarespacescheduling.com"],
    "HubSpot Meetings":  ["meetings.hubspot.com", "meetings.hs.com"],
    "Cal.com":           ["cal.com/"],
    "Tidycal":           ["tidycal.com"],
    "SimplyBook":        ["simplybook.me", "simplybook.it"],
    "Doodle":            ["doodle.com"],
    "Setmore":           ["setmore.com"],
    "Typeform":          ["typeform.com"],
    "Jotform":           ["jotform.com", "jotfor.ms"],
    "Google Forms":      ["docs.google.com/forms", "forms.gle"],
    "Tally":             ["tally.so"],
    "Paperform":         ["paperform.co"],
    "HubSpot Forms":     ["hsforms.com", "hsforms.net"],
    "ActiveCampaign":    ["activehosted.com"],
    # Саппорт-чаты скрыты — не конверсионный канал, шум для tracking-аудита (Zendesk тоже убран):
    # "Intercom":          ["intercom.io", "widget.intercom.io"],
    # "Drift":             ["drift.com", "js.driftt.com"],
    # "Crisp":             ["crisp.chat"],
    # "Tidio":             ["tidio.co"],
    # "Freshchat":         ["freshchat.com", "wchat.freshchat.com"],
    "Stripe":            ["js.stripe.com", "checkout.stripe.com"],
    "Paddle":            ["paddle.com"],
    "Gumroad":           ["gumroad.com"],
    "Pipedrive":         ["pipedrivewebforms.com"],
    "Microsoft Clarity": ["clarity.ms/collect", "clarity.ms/s/"],
}

# Payment services — only via network, never HTML
# (Jotform/other iframes reference Stripe in their CSS/JS → false positives)
NETWORK_ONLY_SERVICES = {"Stripe", "Paddle", "Gumroad"}

ANALYTICS_TOOLS = {"Microsoft Clarity", "Hotjar", "FullStory", "Lucky Orange"}


# ─── Матчинг доменов с границей слова (шаг B, баг A3) ────────────────────────
# Голая подстрока 'cal.com/' матчила 'tetralogiCAL.COM/' внутри бандла AccessiBe
# (pipsnacks: подстрока в Destini-виджете перекрашивала статус страницы в
# фейковую «форму бронирования Cal.com»). Граница слева: перед доменом не может
# стоять буква/цифра/дефис — легитимные 'app.cal.com' (точка) и '//cal.com'
# проходят, 'tetralogical.com' (буква) отбрасывается.
# Tested: 2026-07-13 test_detection.py — tetralogiCAL.COM/ не матчится,
#         app.cal.com/ и "https://cal.com/..." матчятся.

def _boundary_pattern(domain: str):
    # (?<=%2f) — URL-encoded слэш: '?redirect=https%3A%2F%2Fcalendly.com' легитимен,
    # но после lower() перед доменом стоит 'f' и голый lookbehind его резал
    # (ревью дня 6). Оба lookbehind фиксированной ширины — re это допускает.
    return re.compile(r"(?:(?<=%2f)|(?<![a-z0-9-]))" + re.escape(domain.lower()))


_SERVICE_PATTERNS = {
    service: [_boundary_pattern(d) for d in domains]
    for service, domains in EXTERNAL_SERVICES.items()
}
# Те же границы для network-матчинга пиксель-правил (URL всегда лверкейсим)
_PIXEL_DOMAIN_PATTERNS = {
    platform: [_boundary_pattern(d) for d in rule["domains"]]
    for platform, rule in PIXEL_RULES.items()
}


def match_pixel_platform(url: str):
    """Платформа по network-правилам (границы слов + lowercase) или None.
    ЕДИНЫЙ матчер для load-фазы (make_listeners) и клик-фазы (clicker) —
    до 2026-07-13 кликер матчил голой подстрокой по оригинальному URL и
    расходился с load-фазой в обе стороны (ревью дня 6)."""
    u = url.lower()
    for platform, patterns in _PIXEL_DOMAIN_PATTERNS.items():
        for p in patterns:
            if p.search(u):
                return platform
    return None


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_event_from_url(url: str, platform: str) -> str:
    log_fire(f"get_event_from_url: start platform={platform} url={url}")
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        rule = PIXEL_RULES.get(platform, {})
        ep = rule.get("event_param")
        if ep and ep in params:
            log_fire(f"get_event_from_url: {platform} event_param '{ep}' -> '{params[ep][0]}'")
            return params[ep][0]
        log_fire(f"get_event_from_url: {platform} no event_param match (ep={ep}) -> 'fired'")
    except Exception as e:
        log_debug(f"get_event_from_url: parse failed for {url}: {e}")
    return "fired"


def _event_from_body(body: str, ep: str) -> str:
    """Значение поля `ep` из POST-тела запроса; '' если не найдено.

    Три формата тел, подтверждённые на 59 фикстурах scans/_fixtures/post_bodies
    (BUGS-2026-07-13, Проблема 1):
      JSON        (TikTok):  {"event":"AddToCart", ...}
      multipart   (Meta):    ...; name="ev"<CRLF><CRLF>AddToCart<CRLF>--boundary
      urlencoded  (Meta):    id=..&ev=AddToCart&..
    Границы ключа (кавычки JSON / name="ev" / parse_qs), не голая подстрока —
    не цепляет event_id, page_trigger, поля se/ss/pmd с JSON-значениями. Regex,
    а не json.loads всего документа — устойчиво к обрезке тела."""
    stripped = body.lstrip()
    # 1) JSON (TikTok; Meta CAPI на будущее) — только точный ключ, не event_id
    if stripped[:1] in ("{", "["):
        m = re.search(r'"' + re.escape(ep) + r'"\s*:\s*"([^"]*)"', body)
        return m.group(1) if m else ""
    # 2) multipart/form-data (Meta sendBeacon). [\r\n]+ покрывает \n, \r\n и
    #    реальный \r\r\n фикстур; якорь name="ev" не цепляет se/ss/pmd.
    if stripped.startswith("--") or "Content-Disposition" in body[:200]:
        m = re.search(r'name="' + re.escape(ep) + r'"[\r\n]+([^\r\n]+)', body)
        if m:
            val = m.group(1).strip()
            if val and not val.startswith("--"):   # пустое ev → следующий boundary, не имя
                return val
        return ""
    # 3) application/x-www-form-urlencoded (Meta formPOST)
    try:
        vals = parse_qs(body).get(ep)
        if vals and vals[0]:
            return vals[0]
    except Exception:
        pass
    return ""


def get_event_from_request(request, platform: str) -> str:
    """Имя события с аддитивным дочитыванием POST-тела (BUGS-2026-07-13, Проблема 1).

    Meta шлёт содержательные события multipart/urlencoded POST'ом (name="ev"),
    TikTok — JSON-телом ("event":..). get_event_from_url читает только query →
    такие события деградировали в 'fired' (системный ложный GAP).

    ПИН: если query дал имя (GET-пиксели GA4/Bing/Pinterest; Meta/TikTok, когда
    событие всё же в URL) — возвращаем его, тело НЕ читаем. Тело трогаем ТОЛЬКО
    когда query дал 'fired'."""
    event = get_event_from_url(request.url, platform)
    if event != "fired":
        return event
    ep = PIXEL_RULES.get(platform, {}).get("event_param")
    if not ep:                     # Snapchat (event_param=None) — имя в теле не ищем
        return "fired"
    try:
        body = request.post_data
    except Exception:
        log_debug(f"get_event_from_request: post_data недоступен для {request.url}")
        return "fired"
    if not body:
        return "fired"
    name = _event_from_body(body, ep)
    if name:
        log_fire(f"get_event_from_request: {platform} событие из POST-тела -> '{name}'")
        return name
    log_fire(f"get_event_from_request: {platform} тело без имени события -> 'fired'")
    return "fired"


def get_pixel_id_from_url(url: str, platform: str) -> str:
    """Достаёт ID пикселя/счётчика из tracking-URL. '' если нет.
    Path-regex (Meta SDK config, Google Ads) ловит ID даже когда конверсионное
    событие не стрельнуло — например при пассивной загрузке без действия пользователя."""
    log_fire(f"get_pixel_id_from_url: start platform={platform} url={url}")
    rule = PIXEL_RULES.get(platform, {})
    try:
        parsed = urlparse(url)
        id_re = rule.get("id_path_re")
        if id_re:
            m = re.search(id_re, parsed.path)
            if m:
                log_fire(f"get_pixel_id_from_url: {platform} id via path-regex -> '{m.group(1)}'")
                return m.group(1)
        ip = rule.get("id_param")
        if ip:
            params = parse_qs(parsed.query)
            if ip in params and params[ip][0]:
                log_fire(f"get_pixel_id_from_url: {platform} id via query-param '{ip}' -> '{params[ip][0]}'")
                return params[ip][0]
        log_fire(f"get_pixel_id_from_url: {platform} no id found")
    except Exception as e:
        log_debug(f"get_pixel_id_from_url: parse failed for {url}: {e}")
    return ""


def is_conversion_event(platform: str, event: str) -> bool:
    return any(c.lower() in event.lower() for c in CONVERSION_EVENTS_TIER1.get(platform, []))


def is_partial_event(platform: str, event: str) -> bool:
    return any(c.lower() in event.lower() for c in CONVERSION_EVENTS_TIER2.get(platform, []))


def is_noise_event(platform: str, event: str) -> bool:
    return event in NOISE_EVENTS.get(platform, [])


def detect_external_services(html: str, requests_urls: list = None) -> dict:
    log_debug(f"detect_external_services: start html_len={len(html)} n_requests={len(requests_urls) if requests_urls else 0}")
    found = {}
    html_lower = html.lower()
    # матчинг с границей слова (_SERVICE_PATTERNS) — не голой подстрокой (баг A3)
    for service, domains in EXTERNAL_SERVICES.items():
        if service in NETWORK_ONLY_SERVICES:
            log_debug(f"detect_external_services: skip {service} (network-only) in HTML pass")
            continue
        for domain, pattern in zip(domains, _SERVICE_PATTERNS[service]):
            if pattern.search(html_lower):
                log_debug(f"detect_external_services: {service} matched in HTML via '{domain}'")
                found[service] = {"detected_via": "html", "domain": domain}
                break
    if requests_urls:
        for req_url in requests_urls:
            req_lower = req_url.lower()
            for service, domains in EXTERNAL_SERVICES.items():
                if service not in found:
                    for domain, pattern in zip(domains, _SERVICE_PATTERNS[service]):
                        if pattern.search(req_lower):
                            log_debug(f"detect_external_services: {service} matched in network via '{domain}'")
                            found[service] = {"detected_via": "network", "domain": domain}
                            break
    log_debug(f"detect_external_services: done, found {len(found)} service(s): {sorted(found)}")
    return found


# ─── Pixel hits — сырьё улик для испытательного стенда ───────────────────────
# Meta шлёт содержательные события multipart-POST'ом, TikTok всё в JSON-телах
# (BUGS-2026-07-13) — network_requests (только URL) для улик стенда недостаточно.
# capture_pixel_hit пишет {url, method, body_snippet} для запросов к известным
# пиксель-хостам. Это НЕ детекция (не влияет на вердикты сканера) — только сырьё
# для eval_lib.find_event_evidence / гейта Rodion'а. См. TESTBED-PLAN.md.

PIXEL_HIT_HOSTS = (
    "facebook.com/tr", "connect.facebook.net",
    "analytics.tiktok.com",
    "ct.pinterest.com",
    "tr.snapchat.com", "sc-static.net",
    "bat.bing.com",
    "google-analytics.com", "analytics.google.com",
    "googleadservices.com", "googleads.g.doubleclick.net",
    "google.com/ccm/collect", "google.com/pagead", "google.com/rmkt",
    "google.com/measurement", "googletagmanager.com/gtag",
    "px.ads.linkedin.com", "snap.licdn.com",
)

PIXEL_HIT_CAP = 200          # запросов на страницу
PIXEL_HIT_URL_CAP = 1000     # символов URL — event-параметр (en=/ev=) у GET-пикселей
                             # живёт глубоко в query, 300 символов его отрезало (ревью 2026-07-13)
PIXEL_HIT_BODY_CAP = 3000    # символов тела (Meta multipart ~2KB — влезает целиком)


def capture_pixel_hit(request, out: list):
    """Записать запрос к пиксель-хосту с методом и телом. Тихо молчит на прочем.

    Известное ограничение: слушатели сканеров снимаются ДО клик-фазы (кликер
    держит собственный listener), поэтому клик-события сюда не попадают —
    их улики собирает witness.py --journey (день 3 плана, TESTBED-PLAN.md)."""
    url = request.url
    if len(out) >= PIXEL_HIT_CAP or not any(h in url for h in PIXEL_HIT_HOSTS):
        return
    body = None
    try:
        body = request.post_data
    except Exception:
        pass  # бинарное/недоступное тело — фиксируем хотя бы url+method
    out.append({
        "url": url[:PIXEL_HIT_URL_CAP],
        "method": request.method,
        "body_snippet": (body or "")[:PIXEL_HIT_BODY_CAP] or None,
    })


# ─── Navigate & gate — редиректы и мёртвые страницы (день 7, C8/C9) ──────────
# C8 (fritz-kola): все пути .de 301-ят на fritz-kola.com/ — сканер трижды
# аудировал одну главную под видом cart/contact/product и не замечал.
# C9 (bombas): 404-страницы аудировались как живые.
# Правила (гейт-раунды 2026-07-13):
# - смена РЕГИСТРИРУЕМОГО домена (последние 2 метки) = редирект-блок
#   (fritz-kola.de→.com — да; us.checkout.gymshark.com→www.gymshark.com — НЕТ,
#   георедирект внутри одного домена контент не подменяет)
# - схлопывание непустого пути в '/' = редирект-блок
# - трейлинг-слэш, http→https, языковой префикс (/→/en, tinytronics: Rodion
#   решил «норма») — НЕ редирект
# - HTTP-статус финального документа >= 400 = мёртвая страница
# Ограничение: «последние 2 метки» наивны для co.uk-подобных зон — для корпуса
# (.com/.de/.nl/.fr/.ca/.ie/.shop/.ai) достаточно.


def _registrable(host: str) -> str:
    parts = (host or "").lower().split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def gate_verdict(requested_url: str, final_url: str, http_status) -> dict:
    """Чистая функция вердикта шлюза — тестируется без браузера (test_detection)."""
    out = {"http_status": http_status, "final_url": (final_url or "")[:300],
           "redirected": False, "http_error": False}
    if isinstance(http_status, int) and http_status >= 400:
        out["http_error"] = True
    if not final_url:
        return out
    req, fin = urlparse(requested_url), urlparse(final_url)
    host_changed = bool(fin.netloc) and _registrable(req.netloc) != _registrable(fin.netloc)
    req_path = req.path.rstrip("/") or "/"
    fin_path = fin.path.rstrip("/") or "/"
    path_collapsed = req_path != "/" and fin_path == "/"
    if host_changed or path_collapsed:
        out["redirected"] = True
    return out


def navigate_and_gate(page, url: str, settle_ms: int = 1500, retry_settle_ms: int = 1000) -> dict:
    """goto с ретраем (общий паттерн трёх сканеров) + вердикт шлюза.
    Возвращает gate_verdict(...) + 'errors' (список ошибок навигации)."""
    errors = []
    response = None
    try:
        log_debug(f"navigate_and_gate: goto try1 (20s) {url}")
        response = page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(settle_ms)
    except Exception as e:
        log_debug(f"navigate_and_gate: try1 failed ({str(e)[:80]}) — retry 10s")
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=10000)
            page.wait_for_timeout(retry_settle_ms)
        except Exception as e2:
            log_warn(f"navigate_and_gate: navigation failed for {url}: {str(e2)[:100]}")
            errors.append(str(e2)[:100])
    status = None
    try:
        status = response.status if response else None
    except Exception:
        pass
    final_url = ""
    try:
        final_url = page.url or ""
    except Exception:
        pass
    verdict = gate_verdict(url, final_url, status)
    verdict["errors"] = errors
    if verdict["redirected"] or verdict["http_error"]:
        log_debug(f"navigate_and_gate: ШЛЮЗ redirected={verdict['redirected']} "
                  f"http_status={status} final={final_url[:80]}")
    return verdict


def gated_result(url: str, page_type: str, gate: dict) -> dict:
    """Минимальный результат для страницы, не прошедшей шлюз: аудита не было,
    все детекционные поля пустые. Статус проставит step2 (⛔/↪)."""
    return {
        "url": url, "path": urlparse(url).path or "/", "page_type": page_type,
        "gate": gate,
        "pixel_events": {}, "pixel_ids": {},
        "conversion_events_found": [], "partial_events_found": [],
        "missing_events": [], "only_noise_events": False,
        "external_services": {}, "network_requests": [], "pixel_hits": [],
        "cta_buttons": [], "cta_elements": [], "ctas_in_html": {},
        "has_cta": False, "has_iframe_form": False, "iframe_forms": [],
        "forms_count": 0, "shopify_pixel_platforms": [],
        "content_analysis": {"ctas": {}, "forms_count": 0,
                             "is_page_of_interest": False, "poi_reason": ""},
        "errors": list(gate.get("errors", [])),
    }


# ─── Network listeners ────────────────────────────────────────────────────────

def make_listeners(pixel_events: dict, web_pixel_urls: list, web_pixel_bodies: dict,
                   all_html_parts: list, pixel_ids: dict = None):
    """Returns (on_request, on_response) closures that populate shared dicts."""
    if pixel_ids is None:
        pixel_ids = {}

    def on_request(request):
        req_url = request.url
        if "/web-pixels" in req_url:
            log_fire(f"on_request: web-pixels asset captured url={req_url}")
            web_pixel_urls.append(req_url)
        req_url_lower = req_url.lower()
        for platform, rules in PIXEL_RULES.items():
            # границы слова (шаг B): защита от подстрочных совпадений внутри чужих доменов
            for domain, pattern in zip(rules["domains"], _PIXEL_DOMAIN_PATTERNS[platform]):
                if pattern.search(req_url_lower):
                    log_fire(f"on_request: {platform} pixel request matched domain '{domain}' url={req_url}")
                    # POST-тело дочитывается только когда query пуст (BUGS-2026-07-13, Проблема 1)
                    event = get_event_from_request(request, platform)
                    pixel_events.setdefault(platform, [])
                    entry = {
                        "event": event,
                        "is_conversion": is_conversion_event(platform, event),
                        "is_partial": is_partial_event(platform, event),
                        "is_noise": is_noise_event(platform, event),
                    }
                    log_fire(f"on_request: {platform} event='{event}' conversion={entry['is_conversion']} partial={entry['is_partial']} noise={entry['is_noise']}")
                    if not any(e["event"] == event for e in pixel_events[platform]):
                        log_fire(f"on_request: {platform} new event '{event}' recorded")
                        pixel_events[platform].append(entry)
                    # Собираем ID пикселя — для presence (headless) и детекта дублей
                    pid = get_pixel_id_from_url(req_url, platform)
                    if pid:
                        pixel_ids.setdefault(platform, [])
                        if pid not in pixel_ids[platform]:
                            log_fire(f"on_request: {platform} new pixel id '{pid}' recorded")
                            pixel_ids[platform].append(pid)
                    break

    def on_response(response):
        try:
            # Редиректы (3xx) — тела нет, читать незачем (избегаем холостых попыток + шума).
            if response.status >= 300:
                return
            ct = response.headers.get("content-type", "")
            url = response.url
            if "javascript" in ct or "html" in ct:
                try:
                    body = response.body()
                    text = body.decode("utf-8", errors="ignore")
                    all_html_parts.append(text)
                    if "/web-pixels" in url:
                        log_fire(f"on_response: web-pixels body captured url={url} len={len(text)}")
                        web_pixel_bodies[url] = text
                except Exception as e:
                    log_debug(f"on_response: body read/decode failed url={url}: {e}")
        except Exception as e:
            log_debug(f"on_response: header access failed: {e}")

    return on_request, on_response


# ─── Base page scan ───────────────────────────────────────────────────────────

def capture_page_eye(page, combined_html: str = "") -> dict:
    """«Глаз» рядом с кликером: пока кликер жмёт, читаем ЧТО за страница —
    с ОТРИСОВАННОГО DOM (JS выполнен; статика этого не видит).
    Снимаем: title/meta/H1 рендера, строки тарифов/цен («Free for small teams»,
    «per member», «contact sales» — прямой ответ «кому продают»), выжимку текста.
    Никогда не роняет скан — при любой ошибке возвращает {}."""
    eye = {}
    try:
        if page is not None:
            data = page.evaluate(
                """() => {
                    const meta = document.querySelector('meta[name="description"]');
                    const h1s = [...document.querySelectorAll('h1')].slice(0, 4)
                        .map(h => (h.innerText || '').trim()).filter(Boolean);
                    const body = document.body ? document.body.innerText : '';
                    return {title: document.title || '',
                            meta: meta ? (meta.content || '') : '',
                            h1s: h1s, body: body.slice(0, 20000)};
                }""")
            body = data.get("body") or ""
            plan_rx = re.compile(
                r"per\s+(seat|user|member)|/\s?mo\b|per\s+month|free\s+plan|"
                r"free\s+for\b|contact\s+sales|\$\s?\d|€\s?\d|£\s?\d|billed\s+",
                re.IGNORECASE)
            plan_lines = []
            for line in body.splitlines():
                line = line.strip()
                if 3 <= len(line) <= 120 and plan_rx.search(line):
                    plan_lines.append(line)
                if len(plan_lines) >= 10:
                    break
            eye = {
                "title": (data.get("title") or "")[:200],
                "meta_description": (data.get("meta") or "")[:300],
                "h1s": (data.get("h1s") or [])[:4],
                "plan_lines": plan_lines,
                "text_excerpt": " ".join(body.split())[:1500],
            }
    except Exception as e:
        log_debug(f"capture_page_eye: не снялся ({str(e)[:60]}) — пустой глаз")
    return eye


def base_scan_page(page, url: str, page_type: str, expect_events: list,
                   platform: str = "unknown",
                   pixel_events: dict = None,
                   web_pixel_urls: list = None,
                   web_pixel_bodies: dict = None,
                   all_html_parts: list = None,
                   pixel_ids: dict = None,
                   extra_html: str = "") -> dict:
    """
    Core scan logic shared by all platform scanners.
    Callers attach listeners before calling this, or pass pre-populated dicts.
    """
    log_debug(f"base_scan_page: start url={url} page_type={page_type} platform={platform} expect_events={expect_events}")
    pixel_events    = pixel_events    or {}
    web_pixel_urls  = web_pixel_urls  or []
    web_pixel_bodies= web_pixel_bodies or {}
    all_html_parts  = all_html_parts  or []
    pixel_ids       = pixel_ids       or {}

    if extra_html:
        log_debug(f"base_scan_page: appending extra_html len={len(extra_html)}")
        all_html_parts.append(extra_html)

    combined_html = "\n".join(all_html_parts)
    log_debug(f"base_scan_page: combined_html len={len(combined_html)} from {len(all_html_parts)} part(s); classifying content")
    content_analysis = classify_page_content(combined_html, page)
    page_eye = capture_page_eye(page, combined_html)

    # Build request URL list for network-only service detection
    request_urls = list(web_pixel_urls)  # subclasses can extend this

    external_services = detect_external_services(combined_html, request_urls)

    # Aggregate events
    conversion_events_found = []
    partial_events_found = []
    noise_only = True

    for plat, events in pixel_events.items():
        for ev in events:
            if ev["is_conversion"]:
                log_debug(f"base_scan_page: conversion event {plat}:{ev['event']}")
                conversion_events_found.append(f"{plat}:{ev['event']}")
                noise_only = False
            elif ev.get("is_partial"):
                log_debug(f"base_scan_page: partial event {plat}:{ev['event']}")
                partial_events_found.append(f"{plat}:{ev['event']}")
                noise_only = False
            elif not ev["is_noise"]:
                log_debug(f"base_scan_page: non-noise event {plat}:{ev['event']} (clears noise_only)")
                noise_only = False

    missing_events = []
    for expected in expect_events:
        found = False
        for plat, events in pixel_events.items():
            if any(expected.lower() in e["event"].lower() for e in events):
                found = True
                break
        if not found:
            log_debug(f"base_scan_page: expected event '{expected}' not found")
            missing_events.append(expected)

    has_conv = len(conversion_events_found) > 0

    if has_conv:
        log_debug(f"base_scan_page: status OK — {len(conversion_events_found)} conversion event(s): {conversion_events_found}")
        status = "✅ OK"
    else:
        log_debug(f"base_scan_page: status GAP — no conversion events (partial={partial_events_found}, noise_only={noise_only})")
        status = "🚨 GAP"

    return {
        "url": url,
        "path": urlparse(url).path or "/",
        "page_type": page_type,
        "status": status,
        "conversion_events_found": conversion_events_found,
        "partial_events_found": partial_events_found,
        "missing_events": missing_events,
        "only_noise_events": noise_only,
        # noise-события СОХРАНЯЕМ (SDK-load 'fired', page_view и т.п.): по ним step2/report
        # показывают ПРИСУТСТВИЕ пикселя («Платформы: TikTok ✅»), а conversion-логика
        # смотрит на is_noise-флаги и с событиями их не путает. Старый фильтр выкидывал
        # noise-only платформы целиком → bobbies: TikTok[fired] пойман, но «TikTok ❌».
        # Tested: 2026-07-08 on bobbies.com/en/contact
        "pixel_events": {plat: evs for plat, evs in pixel_events.items() if evs},
        "pixel_ids": {p: ids for p, ids in pixel_ids.items() if ids},
        "duplicate_pixels": [p for p, ids in pixel_ids.items() if len(ids) >= 2],
        "external_services": external_services,
        "content_analysis": content_analysis,
        # «глаз»: самоописание ОТРИСОВАННОЙ страницы (для business_type вердикта)
        "page_eye": page_eye,
        "errors": [],
    }
