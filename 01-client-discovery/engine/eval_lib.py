"""
TNC Testbed — eval_lib: чистые функции сравнения скана с эталоном
==================================================================
Сердце испытательного стенда (см. TESTBED-PLAN.md). Здесь НЕТ I/O кроме
load-хелперов и НЕТ логирования — только сравнение структур. Раннер
(eval_run.py) и куратор (make_expected.py) зовут функции отсюда.

Термины:
- эталон (expected)  — golden/expected_<domain>.json, правда проверенная человеком
- MATCH              — скан совпал с эталоном
- FAIL               — расхождение, и улики в сырых network-запросах доказывают
                       что ослеп/сфабриковал СКАНЕР (регрессия — валит прогон)
- DRIFT              — расхождение без улик → похоже сайт сам изменился
                       (жёлтая пометка «перепроверь и обнови эталон», не валит)
- DRIFT_NEW          — на сайте появилось что-то, чего нет в эталоне, улики есть
                       (сайт добавил трекинг — предложить обновить эталон)

Правила сравнения (из golden/README.md):
- status / has_cta / page_type / counters — точное совпадение;
- platforms_detected / gtm_platforms / external_services — «ожидаемое ⊆ фактическое»,
  лишнее фактическое разбирает FAIL/DRIFT-логика;
- *_forbidden — ассерция отсутствия (пины на false positives: Cal.com, Snapchat);
- отсутствующее в эталоне поле = не проверяется.
"""

import re
import json
from pathlib import Path

# ─── Пути ────────────────────────────────────────────────────────────────────

ENGINE_DIR = Path(__file__).resolve().parent
GOLDEN_DIR = ENGINE_DIR / "golden"

# ─── Вердикты ────────────────────────────────────────────────────────────────

MATCH = "MATCH"
FAIL = "FAIL"
DRIFT = "DRIFT"
DRIFT_NEW = "DRIFT_NEW"

# ─── Нормализация статусов ───────────────────────────────────────────────────
# step2.json хранит emoji-строки ('✅ OK', '🚨 GAP', '⚠️ пиксель установлен, ...').
# Эталон хранит нормализованные имена. Маппинг — по префиксу-эмодзи, потому что
# текст после ⚠️ варьируется (два разных unverified-статуса в step2_scan.py:261,272).
# REDIRECTED / HTTP_ERROR — будущие статусы (день 6 плана, navigate_and_gate).

_STATUS_BY_PREFIX = [
    ("✅", "OK"),
    ("🚨", "GAP"),
    ("❌", "NO_TRACKING"),
    ("➖", "NO_CTA"),
    ("⚠", "UNVERIFIED"),   # без variation selector — '⚠️' начинается с '⚠'
    ("↪", "REDIRECTED"),
    ("⛔", "HTTP_ERROR"),
]

KNOWN_STATUSES = {name for _, name in _STATUS_BY_PREFIX}


def normalize_status(raw) -> str:
    """'🚨 GAP' → 'GAP'. Уже нормализованное ('GAP') проходит как есть."""
    if raw is None:
        return "MISSING"
    s = str(raw).strip()
    if s in KNOWN_STATUSES:
        return s
    for prefix, name in _STATUS_BY_PREFIX:
        if s.startswith(prefix):
            return name
    return f"UNKNOWN:{s[:40]}"


# ─── Канонические имена платформ ─────────────────────────────────────────────
# В step2.json одна и та же платформа живёт под разными именами в разных полях
# (GTM-имена vs scan-имена, см. GTM_TO_SCAN в step2_scan.py:84). Эталон — только
# канонические имена.

PLATFORM_ALIASES = {
    "Meta Pixel": "Meta",
    "Facebook": "Meta",
    "Facebook Pixel": "Meta",
    "Google Analytics GA4": "Google Analytics",
    "GA4": "Google Analytics",
    "TikTok Pixel": "TikTok",
    "LinkedIn Insight": "LinkedIn",
    "Microsoft/Bing": "Bing/Microsoft",
    "Bing": "Bing/Microsoft",
    "Microsoft Ads": "Bing/Microsoft",
    "Snapchat Pixel": "Snapchat",
    "Pinterest Tag": "Pinterest",
}


def canonical_platform(name) -> str:
    return PLATFORM_ALIASES.get(str(name).strip(), str(name).strip())


def canonical_set(names) -> set:
    return {canonical_platform(n) for n in (names or [])}


def canonical_event(event) -> str:
    """'Meta Pixel:AddToCart' → 'Meta:AddToCart' (канонизация префикса платформы,
    имя события не трогаем — регистр значим: AddToCart vs add_to_cart)."""
    s = str(event).strip()
    if ":" not in s:
        return s
    plat, _, name = s.partition(":")
    return f"{canonical_platform(plat)}:{name.strip()}"


# ─── Извлечение фактов из страницы step2.json ────────────────────────────────

def page_platforms(page: dict) -> set:
    """Платформы, которые сканер задетектил на странице:
    pixel_events ∪ pixel_ids ∪ shopify_pixel_platforms ∪ префиксы пойманных
    конверсий (канонизировано). Последнее — потому что платформа может быть
    видна ТОЛЬКО через пойманное событие: tinytronics '/' держит GA4 в
    conversion_events_found ('Google Analytics:add_to_cart'), а в pixel_events
    его нет (кейс из ревью 2026-07-13)."""
    found = set()
    found |= canonical_set((page.get("pixel_events") or {}).keys())
    found |= canonical_set((page.get("pixel_ids") or {}).keys())
    found |= canonical_set(page.get("shopify_pixel_platforms") or [])
    for ev_name in page.get("conversion_events_found") or []:
        if ":" in str(ev_name):
            found.add(canonical_platform(str(ev_name).split(":", 1)[0]))
    return found


def code_only_platforms(page: dict) -> set:
    """Платформы, задетекченные ТОЛЬКО по html-маркерам web-pixel кода
    (shopify_pixel_platforms) без единого сетевого следа в полях сканера.
    Это легитимная детекция (A1-фикс пометил её warning'ом в отчётах),
    НЕ фабрикация — запросы могли не полететь из-за consent/паузы."""
    network_backed = set()
    network_backed |= canonical_set((page.get("pixel_events") or {}).keys())
    network_backed |= canonical_set((page.get("pixel_ids") or {}).keys())
    for ev_name in page.get("conversion_events_found") or []:
        if ":" in str(ev_name):
            network_backed.add(canonical_platform(str(ev_name).split(":", 1)[0]))
    return canonical_set(page.get("shopify_pixel_platforms") or []) - network_backed


def page_external_services(page: dict) -> set:
    """Имена внешних сервисов. В реальных файлах встречаются ТРИ формы
    (проверено на архивах 2026-07-13): dict {имя: детали}, список строк
    (garage апрель-2026), список словарей {'name': ...}. Понимаем все."""
    field = page.get("external_services") or []
    names = set()
    if isinstance(field, dict):
        return {str(k).strip() for k in field.keys() if str(k).strip()}
    for svc in field:
        if isinstance(svc, dict):
            name = svc.get("name") or svc.get("service") or ""
        else:
            name = str(svc)
        if name:
            names.add(name.strip())
    return names


# ─── Пробы улик ──────────────────────────────────────────────────────────────
# НАМЕРЕННО независимы от продакшн-кода детекции (PIXEL_RULES / platforms.py):
# если рефакторинг сломает реестр, стенд не должен ослепнуть вместе с ним.
# Простые подстроки по хостам — этого достаточно для вопроса «запрос к платформе
# вообще летел?». Матчим и по URL (network_requests, все методы), и по
# pixel_hits {url, method, body_snippet} — Meta шлёт события multipart-POST'ом,
# TikTok всё в JSON-телах (BUGS-2026-07-13), URL-проб для событий недостаточно.

#
# Consent-mode: Google шлёт пинги через общие endpoints (google.com/ccm/collect,
# googletagmanager.com/gtag/destination) — они НЕ различают GA vs Ads, поэтому
# лежат в обоих списках. Улика «Google-стек жив» важнее точной атрибуции:
# в FAIL-сообщение попадает сам URL, человек рассудит.
# Tested: 2026-07-13 на tinytronics.nl — GA4 ходит ТОЛЬКО через ccm/collect +
# gtag/destination (ни одного g/collect), старые пробы давали None при живом GA4.

EVIDENCE_PROBES = {
    "Meta": ["facebook.com/tr", "connect.facebook.net"],
    "Google Analytics": ["google-analytics.com/g/collect", "google-analytics.com/collect",
                         "analytics.google.com/g/collect", "/gtag/js?id=G-",
                         "google.com/ccm/collect", "googletagmanager.com/gtag/destination",
                         "rmkt/collect/G-", "viewthroughconversion/G-"],
    "Google Ads": ["googleadservices.com", "googleads.g.doubleclick.net",
                   "google.com/pagead/", "/gtag/js?id=AW-", "google.com/rmkt/collect/",
                   "google.com/ccm/collect", "googletagmanager.com/gtag/destination"],
    "GTM": ["googletagmanager.com/gtm.js"],
    "TikTok": ["analytics.tiktok.com"],
    "Pinterest": ["ct.pinterest.com"],
    "Snapchat": ["tr.snapchat.com", "sc-static.net/scevent"],
    "Bing/Microsoft": ["bat.bing.com"],
    "LinkedIn": ["px.ads.linkedin.com", "snap.licdn.com"],
    "Criteo": ["criteo.com", "criteo.net"],
    "Hotjar": ["hotjar.com", "hotjar.io"],
    "Segment": ["cdn.segment.com", "api.segment.io"],
    "Klaviyo": ["klaviyo.com"],
}


def iter_raw_hits(page: dict):
    """Все сырые записи трафика страницы в едином виде {url, method, body}.
    network_requests — только URL (метод неизвестен, тело пустое);
    pixel_hits — обогащённые записи (появятся в сканерах в день 2 плана)."""
    for url in page.get("network_requests") or []:
        yield {"url": str(url), "method": "", "body": ""}
    for hit in page.get("pixel_hits") or []:
        if isinstance(hit, dict):
            yield {
                "url": str(hit.get("url", "")),
                "method": str(hit.get("method", "")),
                "body": str(hit.get("body_snippet", "") or hit.get("body", "")),
            }


def find_platform_evidence(page: dict, platform: str):
    """Летел ли на странице хоть один запрос к платформе.
    Возвращает пример URL (улику) или None."""
    probes = EVIDENCE_PROBES.get(canonical_platform(platform))
    if not probes:
        return None
    for hit in iter_raw_hits(page):
        for probe in probes:
            if probe in hit["url"]:
                return hit["url"][:160]
    return None


def find_event_evidence(page: dict, event: str):
    """Летело ли событие ('Meta:AddToCart') — ищем токен в URL и POST-телах.

    Два ограничителя против ложных улик (ревью 2026-07-13):
    1. Скоуп по платформе из префикса: смотрим ТОЛЬКО запросы к её хостам
       (EVIDENCE_PROBES). Иначе 'AddToCart' из JSON-тела TikTok засчитывался
       уликой для Meta. Событие без префикса ищется по хостам всех платформ,
       но никогда — в first-party трафике.
    2. Токен матчится по границам слова (регистрозависимо). Иначе 'conversion'
       находился внутри presence-пинга .../viewthroughconversion/... (летит на
       КАЖДОЙ загрузке с Google-тегом), а 'lead' — внутри хоста googleads.

    Известное ограничение: network_requests пишется на фазе загрузки — клик-фазный
    трафик туда не попадает. Для клик-событий улика появится когда pixel_hits
    (день 2 плана) начнёт писать и клик-фазу; до тех пор их регресс даёт DRIFT,
    не FAIL. Возвращает пример-улику или None."""
    platform, _, token = event.rpartition(":")
    token = token.strip()
    if not token:
        return None
    if platform:
        probes = EVIDENCE_PROBES.get(canonical_platform(platform.strip()))
        if not probes:
            return None   # платформа без проб — улик добыть нечем
    else:
        probes = [p for plist in EVIDENCE_PROBES.values() for p in plist]
    token_re = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(token) + r"(?![A-Za-z0-9_])")
    for hit in iter_raw_hits(page):
        if not any(p in hit["url"] for p in probes):
            continue
        if token_re.search(hit["url"]):
            return hit["url"][:160]
        if hit["body"] and token_re.search(hit["body"]):
            return f"{hit['method']} {hit['url'][:100]} … body: …{_around(hit['body'], token)}…"
    return None


def _around(text: str, token: str, span: int = 40) -> str:
    i = text.find(token)
    if i < 0:
        return ""
    return text[max(0, i - span // 2): i + len(token) + span // 2]


# ─── Сравнение ───────────────────────────────────────────────────────────────

def _check(path, field, expected, actual, verdict, note="") -> dict:
    return {"path": path, "field": field, "expected": expected,
            "actual": actual, "verdict": verdict, "note": note}


def compare_page(path: str, expected_page: dict, actual_page: dict) -> list:
    """Сравнить одну страницу. Возвращает список проверок-словарей."""
    checks = []

    # точные поля — расхождение = FAIL по умолчанию (план, таблица вердиктов)
    for field, extractor in (
        ("status", lambda p: normalize_status(p.get("status"))),
        ("page_type", lambda p: p.get("page_type")),
        ("has_cta", lambda p: p.get("has_cta")),
    ):
        if field not in expected_page:
            continue
        want = expected_page[field] if field != "status" else normalize_status(expected_page[field])
        got = extractor(actual_page)
        verdict = MATCH if want == got else FAIL
        checks.append(_check(path, field, want, got, verdict))

    # платформы: ожидаемое ⊆ фактическое; расхождения решают улики
    if "platforms_detected" in expected_page:
        want = canonical_set(expected_page["platforms_detected"])
        got = page_platforms(actual_page)
        for plat in sorted(want - got):
            evidence = find_platform_evidence(actual_page, plat)
            if evidence:
                checks.append(_check(path, f"platform:{plat}", "detected", "missing", FAIL,
                                     f"улика в трафике есть, детекция ослепла: {evidence}"))
            else:
                checks.append(_check(path, f"platform:{plat}", "detected", "missing", DRIFT,
                                     "улик в трафике нет — сайт мог снять пиксель, перепроверь"))
        code_only = code_only_platforms(actual_page)
        has_raw_channel = bool(actual_page.get("network_requests") or actual_page.get("pixel_hits"))
        for plat in sorted(got - want):
            evidence = find_platform_evidence(actual_page, plat)
            if evidence:
                checks.append(_check(path, f"platform:{plat}", "absent", "detected", DRIFT_NEW,
                                     f"сайт добавил трекинг? улика: {evidence}"))
            elif plat in code_only:
                # легитимная html-детекция web-pixel кода (A1-фикс), не фабрикация:
                # запросы могли не полететь из-за consent/паузы
                checks.append(_check(path, f"platform:{plat}", "absent", "detected", DRIFT_NEW,
                                     "детекция code-only (html-маркер web-pixel), запросов нет — перепроверь"))
            elif not has_raw_channel or canonical_platform(plat) not in EVIDENCE_PROBES:
                # канал улик пуст (старая схема без network_requests) или платформа
                # без проб — «фабрикацию» доказать нечем, не валим прогон
                checks.append(_check(path, f"platform:{plat}", "absent", "detected", DRIFT_NEW,
                                     "улик добыть нечем (нет сырья/проб) — перепроверь руками"))
            else:
                checks.append(_check(path, f"platform:{plat}", "absent", "detected", FAIL,
                                     "детекция без единого запроса к платформе — фабрикация"))
        for plat in sorted(want & got):
            checks.append(_check(path, f"platform:{plat}", "detected", "detected", MATCH))

    # пины на false positives: платформа НЕ должна детектиться
    for plat in canonical_set(expected_page.get("platforms_forbidden")):
        got = page_platforms(actual_page)
        if plat in got:
            checks.append(_check(path, f"platform_forbidden:{plat}", "absent", "detected", FAIL,
                                 "запинено как false positive — снова детектится"))
        else:
            checks.append(_check(path, f"platform_forbidden:{plat}", "absent", "absent", MATCH))

    # внешние сервисы: ожидаемое ⊆ фактическое; проб улик для них нет → DRIFT.
    # Лишние фактические — DRIFT_NEW (видимость вместо молчания; ревью 2026-07-13)
    if "external_services" in expected_page:
        want = {s.strip() for s in expected_page["external_services"]}
        got = page_external_services(actual_page)
        for svc in sorted(want - got):
            checks.append(_check(path, f"service:{svc}", "detected", "missing", DRIFT,
                                 "проб улик для сервисов нет — перепроверь глазами"))
        for svc in sorted(got - want):
            checks.append(_check(path, f"service:{svc}", "absent", "detected", DRIFT_NEW,
                                 "новый сервис вне эталона — реально появился или false positive? перепроверь"))
        for svc in sorted(want & got):
            checks.append(_check(path, f"service:{svc}", "detected", "detected", MATCH))

    for svc in expected_page.get("external_services_forbidden") or []:
        got = page_external_services(actual_page)
        if svc in got:
            checks.append(_check(path, f"service_forbidden:{svc}", "absent", "detected", FAIL,
                                 "запинено как false positive (кейс Cal.com) — снова детектится"))
        else:
            checks.append(_check(path, f"service_forbidden:{svc}", "absent", "absent", MATCH))

    # missing_events — точное множество (что отчёт называет отсутствующим)
    if "missing_events" in expected_page:
        want = set(expected_page["missing_events"])
        got = set(actual_page.get("missing_events") or [])
        verdict = MATCH if want == got else FAIL
        checks.append(_check(path, "missing_events", sorted(want), sorted(got), verdict))

    # conversion_events_min — события, которые ОБЯЗАНЫ быть пойманы
    # (формат 'Meta:AddToCart'; POST-правда artbouquet живёт здесь).
    # Сравнение — по каноническим именам ('Meta Pixel:X' == 'Meta:X')
    for ev in expected_page.get("conversion_events_min") or []:
        ev = canonical_event(ev)
        got_events = {canonical_event(e) for e in actual_page.get("conversion_events_found") or []}
        if ev in got_events:
            checks.append(_check(path, f"event:{ev}", "caught", "caught", MATCH))
            continue
        evidence = find_event_evidence(actual_page, ev)
        if evidence:
            checks.append(_check(path, f"event:{ev}", "caught", "missing", FAIL,
                                 f"событие летело, парсер не поймал: {evidence}"))
        else:
            checks.append(_check(path, f"event:{ev}", "caught", "missing", DRIFT,
                                 "улик что событие летело нет — перепроверь"))

    return checks


def compare_site(expected: dict, actual_step2: dict, step1: dict = None) -> dict:
    """Сравнить весь сайт. Возвращает {'checks': [...], 'summary': {...}}."""
    checks = []
    site = expected.get("site") or {}

    # платформа сайта — из замороженного step1 (в step2.json её нет)
    if "platform" in site and step1 is not None:
        want = site["platform"]
        got = ((step1.get("platform") or {}).get("platform")
               if isinstance(step1.get("platform"), dict) else step1.get("platform"))
        checks.append(_check("<site>", "platform", want, got,
                             MATCH if want == got else FAIL))

    # gtm_platforms: ожидаемое ⊆ фактическое; улики ищем по всем страницам
    if "gtm_platforms" in site:
        want = canonical_set(site["gtm_platforms"])
        got = canonical_set(actual_step2.get("gtm_platforms"))
        all_pages = actual_step2.get("all_pages") or []
        for plat in sorted(want - got):
            evidence = None
            for pg in all_pages:
                evidence = find_platform_evidence(pg, plat)
                if evidence:
                    break
            verdict = FAIL if evidence else DRIFT
            note = (f"улика в трафике есть: {evidence}" if evidence
                    else "улик нет — контейнер мог измениться, перепроверь")
            checks.append(_check("<site>", f"gtm:{plat}", "detected", "missing", verdict, note))
        # лишние gtm-платформы — DRIFT_NEW (сигнатура в контейнере без network-улик
        # не доказывает фабрикацию: тег может быть consent-gated; ревью 2026-07-13)
        for plat in sorted(got - want):
            evidence = None
            for pg in all_pages:
                evidence = find_platform_evidence(pg, plat)
                if evidence:
                    break
            note = (f"в контейнере появилась платформа вне эталона; улика: {evidence}" if evidence
                    else "в контейнере появилась платформа вне эталона; network-улик нет — перепроверь")
            checks.append(_check("<site>", f"gtm:{plat}", "absent", "detected", DRIFT_NEW, note))
        for plat in sorted(want & got):
            checks.append(_check("<site>", f"gtm:{plat}", "detected", "detected", MATCH))

    # счётчики — точное совпадение по каждому заявленному ключу
    for key, want in (site.get("counters") or {}).items():
        got = actual_step2.get(key)
        checks.append(_check("<site>", f"counter:{key}", want, got,
                             MATCH if want == got else FAIL))

    # страницы
    actual_by_path = {pg.get("path"): pg for pg in actual_step2.get("all_pages") or []}
    for path, expected_page in (expected.get("pages") or {}).items():
        actual_page = actual_by_path.get(path)
        if actual_page is None:
            checks.append(_check(path, "page", "scanned", "missing", FAIL,
                                 "страница из замороженного step1 не попала в результат"))
            continue
        checks.extend(compare_page(path, expected_page, actual_page))

    summary = summarize(checks)
    return {"checks": checks, "summary": summary}


def summarize(checks: list) -> dict:
    """Счёт по списку проверок: DRIFT_NEW считается как drift."""
    n_match = sum(1 for c in checks if c["verdict"] == MATCH)
    n_fail = sum(1 for c in checks if c["verdict"] == FAIL)
    n_drift = sum(1 for c in checks if c["verdict"] in (DRIFT, DRIFT_NEW))
    total = len(checks)
    return {
        "total": total, "match": n_match, "fail": n_fail, "drift": n_drift,
        "pct": round(100.0 * n_match / total, 1) if total else 100.0,
    }


# ─── Load-хелперы ────────────────────────────────────────────────────────────

def expected_path(domain: str) -> Path:
    return GOLDEN_DIR / f"expected_{domain}.json"


def frozen_step1_path(domain: str) -> Path:
    return GOLDEN_DIR / domain / "step1.json"


def load_corpus() -> dict:
    with open(GOLDEN_DIR / "corpus.json", encoding="utf-8") as f:
        return json.load(f)


def load_expected(domain: str) -> dict:
    with open(expected_path(domain), encoding="utf-8") as f:
        return json.load(f)


def load_json(path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
