"""
Пин-тесты eval_lib — чтобы логика вердиктов стенда не регрессировала.
Каждый тест-кейс привязан к реальному багу из shakedown 2026-07-08 /
BUGS-2026-07-13 (кейс указан в докстринге).

Запуск: cd 01-client-discovery/engine && python -m pytest test_eval_lib.py -q
"""

import eval_lib as ev


# ─── normalize_status ─────────────────────────────────────────────────────────

def test_normalize_status_emoji():
    assert ev.normalize_status("✅ OK") == "OK"
    assert ev.normalize_status("🚨 GAP") == "GAP"
    assert ev.normalize_status("❌ NO TRACKING") == "NO_TRACKING"
    assert ev.normalize_status("➖ NO CTA") == "NO_CTA"


def test_normalize_status_unverified_variants():
    # два разных ⚠️-текста из step2_scan.py:261 и :272 — оба UNVERIFIED
    s1 = "⚠️ пиксель установлен, конверсионное событие не зафиксировано (нужно действие пользователя)"
    s2 = "⚠️ форма бронирования обнаружена (Cal.com). Конверсионное событие при загрузке страницы не зафиксировано."
    assert ev.normalize_status(s1) == "UNVERIFIED"
    assert ev.normalize_status(s2) == "UNVERIFIED"


def test_normalize_status_passthrough_and_unknown():
    assert ev.normalize_status("GAP") == "GAP"           # уже нормализован
    assert ev.normalize_status("REDIRECTED") == "REDIRECTED"  # будущий статус (день 6)
    assert ev.normalize_status(None) == "MISSING"
    assert ev.normalize_status("что-то новое").startswith("UNKNOWN:")


# ─── canonical_platform / page_platforms ─────────────────────────────────────

def test_canonical_platform_aliases():
    assert ev.canonical_platform("Meta Pixel") == "Meta"
    assert ev.canonical_platform("Google Analytics GA4") == "Google Analytics"
    assert ev.canonical_platform("TikTok Pixel") == "TikTok"
    assert ev.canonical_platform("Pinterest") == "Pinterest"  # без алиаса — как есть


def test_page_platforms_union():
    page = {
        "pixel_events": {"Meta Pixel": [{"event": "PageView"}]},
        "pixel_ids": {"Google Ads": ["963953247"]},
        "shopify_pixel_platforms": ["TikTok Pixel"],
    }
    assert ev.page_platforms(page) == {"Meta", "Google Ads", "TikTok"}


def test_external_services_all_three_schemas():
    # в реальных файлах три формы: список строк (garage апрель-2026),
    # список словарей, dict имя→детали (ревью 2026-07-13)
    as_strings = {"external_services": ["Calendly"]}
    as_dicts = {"external_services": [{"name": "Calendly", "detected_via": "html"}]}
    as_mapping = {"external_services": {"Calendly": {"detected_via": "html"}}}
    assert ev.page_external_services(as_strings) == {"Calendly"}
    assert ev.page_external_services(as_dicts) == {"Calendly"}
    assert ev.page_external_services(as_mapping) == {"Calendly"}


def test_page_platforms_includes_conversion_prefixes():
    # tinytronics '/': GA4 виден ТОЛЬКО через пойманное событие — платформа
    # обязана попасть в platforms_detected (ревью 2026-07-13)
    page = {"pixel_events": {"Google Ads": []},
            "conversion_events_found": ["Google Analytics:add_to_cart"]}
    assert ev.page_platforms(page) == {"Google Ads", "Google Analytics"}


# ─── Пробы улик ──────────────────────────────────────────────────────────────

def test_platform_evidence_from_url():
    # кейс B5: ct.pinterest.com бьёт в наши же network_requests (gymshark)
    page = {"network_requests": ["https://ct.pinterest.com/v3/?tid=2618098611272&event=pagevisit"]}
    assert ev.find_platform_evidence(page, "Pinterest")
    assert ev.find_platform_evidence(page, "Meta") is None


def test_platform_evidence_from_pixel_hits_post():
    # POST на facebook.com/tr без query — URL всё равно улика
    page = {"pixel_hits": [{"url": "https://www.facebook.com/tr/", "method": "POST",
                            "body_snippet": 'name="ev"\r\n\r\nViewContent'}]}
    assert ev.find_platform_evidence(page, "Meta")


def test_event_evidence_in_post_body():
    # кейс BUGS-2026-07-13 Проблема 1: Meta шлёт AddToCart multipart-POST'ом,
    # в URL события нет — улика обязана найтись в теле
    page = {
        "network_requests": ["https://www.facebook.com/tr/"],
        "pixel_hits": [{"url": "https://www.facebook.com/tr/", "method": "POST",
                        "body_snippet": 'Content-Disposition: form-data; name="ev"\r\n\r\nAddToCart\r\n'}],
    }
    assert ev.find_event_evidence(page, "Meta:AddToCart")
    assert ev.find_event_evidence(page, "Meta:Purchase") is None


def test_event_evidence_in_url_query():
    # GA4 кладёт en= в query даже у POST — URL-путь тоже работает
    page = {"network_requests": ["https://region1.google-analytics.com/g/collect?v=2&en=add_to_cart"]}
    assert ev.find_event_evidence(page, "Google Analytics:add_to_cart")


def test_event_evidence_word_boundary_no_false_hit():
    # ревью 2026-07-13 (репро на реальном tinytronics): presence-пинг
    # .../viewthroughconversion/963.../ летит на КАЖДОЙ загрузке — токен
    # 'conversion' внутри него НЕ улика; а вот /pagead/conversion/ — улика
    ping = {"network_requests": [
        "https://googleads.g.doubleclick.net/pagead/viewthroughconversion/963953247/?random=1"]}
    real = {"network_requests": [
        "https://googleads.g.doubleclick.net/pagead/conversion/963953247/?label=x"]}
    assert ev.find_event_evidence(ping, "Google Ads:conversion") is None
    assert ev.find_event_evidence(real, "Google Ads:conversion")


def test_event_evidence_scoped_to_platform_hosts():
    # ревью 2026-07-13: 'AddToCart' из JSON-тела TikTok — НЕ улика для Meta
    page = {"pixel_hits": [{"url": "https://analytics.tiktok.com/api/v2/pixel", "method": "POST",
                            "body_snippet": '{"event": "AddToCart", "properties": {}}'}]}
    assert ev.find_event_evidence(page, "Meta:AddToCart") is None
    assert ev.find_event_evidence(page, "TikTok:AddToCart")


def test_event_evidence_lead_not_matched_inside_googleads_host():
    # токен 'lead' — подстрока хоста googLEADs.g.doubleclick.net; границы слова
    # обязаны отбрасывать её (ревью: false FAIL на каждом Google Ads запросе)
    page = {"network_requests": [
        "https://googleads.g.doubleclick.net/pagead/viewthroughconversion/963/?x=1"]}
    assert ev.find_event_evidence(page, "Meta:Lead") is None


def test_platform_evidence_consent_mode_endpoints():
    # Tested: 2026-07-13 на tinytronics.nl — GA4 ходит ТОЛЬКО через
    # google.com/ccm/collect + gtag/destination (ни одного g/collect)
    page = {"network_requests": [
        "https://www.google.com/ccm/collect?rcb=6&en=page_view&dt=TinyTronics",
        "https://www.googletagmanager.com/gtag/destination?id=AW-G-TY2ZCS2MBT&cx=c"]}
    assert ev.find_platform_evidence(page, "Google Analytics")


# ─── compare_page: вердикты ──────────────────────────────────────────────────

def test_status_mismatch_is_fail():
    checks = ev.compare_page("/", {"status": "OK"}, {"status": "🚨 GAP"})
    assert checks == [
        {"path": "/", "field": "status", "expected": "OK", "actual": "GAP",
         "verdict": ev.FAIL, "note": ""},
    ]


def test_expected_platform_missing_with_evidence_is_fail():
    # «детекция ослепла»: пиксель летит в трафике, но page_platforms его не видит
    expected = {"platforms_detected": ["Pinterest"]}
    actual = {"pixel_events": {}, "network_requests": ["https://ct.pinterest.com/v3/?tid=1"]}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.FAIL and c["field"] == "platform:Pinterest"


def test_expected_platform_missing_without_evidence_is_drift():
    # сайт мог реально снять пиксель — не валим прогон
    expected = {"platforms_detected": ["Pinterest"]}
    actual = {"pixel_events": {}, "network_requests": ["https://example.com/app.js"]}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.DRIFT


def test_extra_platform_without_evidence_is_fabrication_fail():
    # кейс A1: «Snapchat ✅» без единого запроса к Snapchat = фабрикация
    expected = {"platforms_detected": []}
    actual = {"pixel_events": {"Snapchat": [{"event": "PageView"}]},
              "network_requests": ["https://cdn.shopify.com/web-pixels-manager.js"]}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.FAIL and "фабрикация" in c["note"]


def test_extra_platform_with_evidence_is_drift_new():
    # сайт реально добавил трекинг — предложить обновить эталон
    expected = {"platforms_detected": []}
    actual = {"pixel_events": {"TikTok": [{"event": "PageView"}]},
              "network_requests": ["https://analytics.tiktok.com/api/v2/pixel"]}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.DRIFT_NEW


def test_code_only_shopify_platform_is_not_fabrication():
    # кейс pipsnacks Pinterest: html-маркер web-pixel кода без запросов —
    # спроектированное поведение сканера (A1-фикс), НЕ фабрикация (ревью 2026-07-13)
    expected = {"platforms_detected": []}
    actual = {"shopify_pixel_platforms": ["Pinterest"], "pixel_events": {},
              "network_requests": ["https://cdn.shopify.com/web-pixels-manager.js"]}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.DRIFT_NEW and "code-only" in c["note"]


def test_extra_platform_without_raw_channel_is_drift_new():
    # старая схема без network_requests: канал улик пуст — «фабрикацию»
    # доказать нечем, не валим прогон (ревью 2026-07-13)
    expected = {"platforms_detected": []}
    actual = {"pixel_events": {"Meta": [{"event": "PageView"}]}}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.DRIFT_NEW and "нечем" in c["note"]


def test_extra_service_is_visible_as_drift_new():
    # лишний сервис вне эталона больше не невидим (ревью 2026-07-13)
    expected = {"external_services": []}
    actual = {"external_services": [{"name": "Cal.com"}]}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.DRIFT_NEW and c["field"] == "service:Cal.com"


def test_conversion_events_min_canonical_prefix():
    # алиасный префикс в эталоне ('Meta Pixel:X') матчится с 'Meta:X' (ревью 2026-07-13)
    expected = {"conversion_events_min": ["Meta Pixel:AddToCart"]}
    actual = {"conversion_events_found": ["Meta:AddToCart"]}
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.MATCH


def test_forbidden_platform_pin():
    # пин false positive: Snapchat на allbirds не должен вернуться
    expected = {"platforms_forbidden": ["Snapchat"]}
    ok = ev.compare_page("/", expected, {"pixel_events": {}})
    bad = ev.compare_page("/", expected, {"pixel_events": {"Snapchat": []}})
    assert ok[0]["verdict"] == ev.MATCH
    assert bad[0]["verdict"] == ev.FAIL


def test_forbidden_service_pin_cal_com():
    # кейс A3: Cal.com из подстроки tetralogiCAL.COM — запинен как forbidden
    expected = {"external_services_forbidden": ["Cal.com"]}
    bad = ev.compare_page("/", expected, {"external_services": [{"name": "Cal.com"}]})
    assert bad[0]["verdict"] == ev.FAIL


def test_conversion_events_min_post_blindness():
    # кейс artbouquet (BUGS-2026-07-13): AddToCart летит POST'ом, парсер не поймал
    # → FAIL с уликой из тела; после фикса POST-парсера событие попадёт в
    # conversion_events_found и проверка сама станет MATCH
    expected = {"conversion_events_min": ["Meta:AddToCart"]}
    actual = {
        "conversion_events_found": [],
        "pixel_hits": [{"url": "https://www.facebook.com/tr/", "method": "POST",
                        "body_snippet": 'name="ev"\r\n\r\nAddToCart'}],
    }
    (c,) = ev.compare_page("/", expected, actual)
    assert c["verdict"] == ev.FAIL and "парсер не поймал" in c["note"]

    fixed = {"conversion_events_found": ["Meta:AddToCart"], "pixel_hits": []}
    (c2,) = ev.compare_page("/", expected, fixed)
    assert c2["verdict"] == ev.MATCH


def test_absent_field_not_checked():
    # отсутствующее в эталоне поле = не проверяется (ноль проверок)
    assert ev.compare_page("/", {}, {"status": "🚨 GAP", "has_cta": True}) == []


# ─── compare_site ────────────────────────────────────────────────────────────

def _mini_expected():
    return {
        "site": {
            "platform": "opencart",
            "gtm_platforms": ["Google Analytics"],
            "counters": {"gaps": 0, "oks": 2},
        },
        "pages": {"/": {"status": "OK", "has_cta": True}},
    }


def _mini_actual():
    return {
        "gtm_platforms": ["Google Analytics"],  # лишние gtm дают DRIFT_NEW — см. отдельный тест
        "gaps": 0, "oks": 2,
        "all_pages": [{"path": "/", "status": "✅ OK", "has_cta": True}],
    }


def _mini_step1():
    return {"platform": {"platform": "opencart", "confidence": "high"}}


def test_compare_site_all_green():
    res = ev.compare_site(_mini_expected(), _mini_actual(), _mini_step1())
    assert res["summary"]["fail"] == 0
    assert res["summary"]["drift"] == 0
    assert res["summary"]["pct"] == 100.0


def test_compare_site_counter_mismatch_is_fail():
    actual = _mini_actual()
    actual["gaps"] = 3
    res = ev.compare_site(_mini_expected(), actual, _mini_step1())
    fails = [c for c in res["checks"] if c["verdict"] == ev.FAIL]
    assert len(fails) == 1 and fails[0]["field"] == "counter:gaps"


def test_compare_site_missing_page_is_fail():
    # кейс замороженного step1: страница обязана быть в результате
    actual = _mini_actual()
    actual["all_pages"] = []
    res = ev.compare_site(_mini_expected(), actual, _mini_step1())
    page_fails = [c for c in res["checks"] if c["field"] == "page"]
    assert len(page_fails) == 1 and page_fails[0]["verdict"] == ev.FAIL


def test_compare_site_extra_gtm_is_drift_new():
    # платформа появилась в контейнере вне эталона — видимость без false FAIL
    # (сигнатура в контейнере ≠ фабрикация: тег может быть consent-gated)
    expected = _mini_expected()
    actual = _mini_actual()
    actual["gtm_platforms"] = ["Google Analytics", "Meta"]
    res = ev.compare_site(expected, actual, _mini_step1())
    extra = [c for c in res["checks"] if c["field"] == "gtm:Meta"]
    assert len(extra) == 1 and extra[0]["verdict"] == ev.DRIFT_NEW


def test_make_expected_merge_pins_and_platform_omitted():
    # --update переносит ручные пины; build_candidate не пишет platform=null
    # (ревью 2026-07-13, оба high/medium)
    import make_expected as mk
    existing = {"pages": {"/": {"status": "OK", "platforms_forbidden": ["Snapchat"],
                                "conversion_events_min": ["Meta:AddToCart"]}}}
    candidate = {"pages": {"/": {"status": "OK"}}}
    assert mk.merge_pins(existing, candidate) == 2
    assert candidate["pages"]["/"]["platforms_forbidden"] == ["Snapchat"]

    cand2 = mk.build_candidate({"all_pages": []}, step1=None)
    assert "platform" not in cand2["site"]


def test_compare_site_gtm_missing_with_pagelevel_evidence_is_fail():
    # gtm-платформа пропала из отчёта, но на странице живой запрос к ней
    expected = _mini_expected()
    actual = _mini_actual()
    actual["gtm_platforms"] = []
    actual["all_pages"][0]["network_requests"] = [
        "https://region1.google-analytics.com/g/collect?v=2&en=page_view"]
    res = ev.compare_site(expected, actual, _mini_step1())
    gtm = [c for c in res["checks"] if c["field"].startswith("gtm:")]
    assert len(gtm) == 1 and gtm[0]["verdict"] == ev.FAIL


def test_summarize_pct():
    checks = [
        {"verdict": ev.MATCH}, {"verdict": ev.MATCH}, {"verdict": ev.MATCH},
        {"verdict": ev.FAIL}, {"verdict": ev.DRIFT_NEW},
    ]
    s = ev.summarize(checks)
    assert s == {"total": 5, "match": 3, "fail": 1, "drift": 1, "pct": 60.0}
