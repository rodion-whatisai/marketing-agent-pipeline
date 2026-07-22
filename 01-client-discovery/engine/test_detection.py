"""
Пин-тесты детекции — шаг B (день 6). Каждый кейс — реальный баг из shakedown /
гейт-раундов; фикс не имеет права регрессировать.

Запуск: cd 01-client-discovery/engine && python -m pytest test_detection.py -q
"""

from scanners.base_scanner import detect_external_services, make_listeners
from scanners.shopify_scanner import META_HTML_RE
from gtm_analyzer import PLATFORM_SIGNATURES
import re


# ─── A3: границы слов в EXTERNAL_SERVICES ────────────────────────────────────

def test_cal_com_not_matched_inside_foreign_domain():
    # кейс pipsnacks/AccessiBe: 'cal.com/' внутри tetralogiCAL.COM — НЕ Cal.com
    html = '<script src="https://tetralogical.com/widget.js"></script>'
    assert "Cal.com" not in detect_external_services(html)


def test_cal_com_matched_legitimately():
    # легитимные формы: голый домен после // и сабдомен после точки
    assert "Cal.com" in detect_external_services('<a href="https://cal.com/rodion/30min">book</a>')
    assert "Cal.com" in detect_external_services('<iframe src="https://app.cal.com/embed"></iframe>')


def test_calendly_still_detected():
    # регрессия-страховка: обычные сервисы после перехода на границы не потерялись
    found = detect_external_services('<script src="https://assets.calendly.com/widget.js">')
    assert found.get("Calendly", {}).get("detected_via") == "html"


# ─── A4: пустой metaPixelId не фабрикует Meta ────────────────────────────────

def test_empty_meta_pixel_id_not_matched():
    # кейс jobs.fritz-kola.de: "metaPixelId":"" рождал фейковую Meta-запись
    assert META_HTML_RE.search('{"metaPixelId":""}') is None
    assert META_HTML_RE.search('{"pixelId":""}') is None


def test_real_meta_pixel_id_matched():
    assert META_HTML_RE.search('{"pixelId":"1689016458013442"}')
    assert META_HTML_RE.search('<script src="https://connect.facebook.net/en_US/fbevents.js">')


# ─── B5: Pinterest теперь регистрируется network-слушателем ──────────────────

class _FakeRequest:
    def __init__(self, url):
        self.url = url
        self.method = "GET"
        self.post_data = None


def test_pinterest_network_hit_registered():
    # кейс gymshark: ct.pinterest.com бил в наши же network_requests, платформа
    # не регистрировалась (правила не было). Rodion подтвердил Pinterest (гейт №2)
    pixel_events, ids = {}, {}
    on_request, _ = make_listeners(pixel_events, [], {}, [], ids)
    on_request(_FakeRequest(
        "https://ct.pinterest.com/user/?event=pagevisit&tid=2618098611272"))
    assert "Pinterest" in pixel_events
    assert pixel_events["Pinterest"][0]["event"] == "pagevisit"
    assert ids.get("Pinterest") == ["2618098611272"] or "2618098611272" in str(ids.get("Pinterest"))


def test_pixel_domain_boundary_no_false_platform():
    # границы слов в PIXEL_RULES: чужой домен с хвостом нашей подстроки не матчится
    pixel_events = {}
    on_request, _ = make_listeners(pixel_events, [], {}, [], {})
    on_request(_FakeRequest("https://notct.pinterest.com.evil.example/x"))
    # 'ct.pinterest.com' предварён буквой 't' → граница блокирует
    assert "Pinterest" not in pixel_events


# ─── Ревью дня 6: единый матчер клик-фазы + %2f-граница + gtm-маппинг ────────

def test_match_pixel_platform_boundary_and_case():
    # клик-фаза теперь матчит тем же матчером, что load-фаза
    from scanners.base_scanner import match_pixel_platform
    assert match_pixel_platform("https://notct.pinterest.com.evil.example/x") is None
    assert match_pixel_platform("https://CT.PINTEREST.COM/v3/?event=checkout") == "Pinterest"
    assert match_pixel_platform("https://www.facebook.com/tr/?id=1&ev=PageView") == "Meta"
    assert match_pixel_platform("https://example.com/style.css") is None


def test_url_encoded_service_still_detected():
    # ревью дня 6: %2F%2Fcalendly.com после lower() начинался с 'f' — lookbehind резал
    html = '<a href="https://x.com/?redirect=https%3A%2F%2Fcalendly.com%2Fdemo">book</a>'
    assert "Calendly" in detect_external_services(html.lower())
    # а чужое слово по-прежнему блокируется
    assert "Cal.com" not in detect_external_services('<script src="https://tetralogical.com/w.js">')


def test_gtm_to_scan_knows_new_platforms():
    # три локальные мини-копии маппинга не знали Snapchat Pixel / Pinterest Tag
    import platforms as P
    m = P.as_gtm_to_scan()
    assert m["Snapchat Pixel"] == "Snapchat"
    assert m["Pinterest Tag"] == "Pinterest"
    assert m["Google Analytics GA4"] == "Google Analytics"
    assert m["Microsoft/Bing"] == "Bing/Microsoft"


def test_report_noise_covers_pagevisit_for_headline_metric():
    # ревью дня 6 (high): Pinterest pagevisit раздувал «X of N имеют пиксель+событие»
    # is_noise-обёртка уехала из generate_report_html (рефактор report_truth 2026-07);
    # инвариант проверяем на первоисточнике — реестре platforms.
    import platforms as P
    noise = P.as_report_noise_events()
    assert "pagevisit" in noise.get("Pinterest", [])
    assert "PageView" in noise.get("Meta", [])
    assert "checkout" not in noise.get("Pinterest", [])


# ─── B6: Snapchat в GTM-сигнатурах ───────────────────────────────────────────

def test_snapchat_gtm_signature_matches_live_container_js():
    # кейс gymshark: живые snaptr-теги в контейнере не репортились
    sigs = PLATFORM_SIGNATURES["Snapchat Pixel"]
    container_js = "snaptr('track','ADD_CART');loadScript('https://sc-static.net/scevent.min.js')"
    assert any(re.search(s, container_js) for s in sigs)


# ─── День 7: вердикт redirect/404-шлюза (C8/C9) ──────────────────────────────

def test_gate_verdict_rules():
    from scanners.base_scanner import gate_verdict
    # C8 fritz-kola: смена регистрируемого домена = редирект-блок
    v = gate_verdict("https://fritz-kola.de/cart", "https://fritz-kola.com/", 200)
    assert v["redirected"] and not v["http_error"]
    # gymshark: георедирект ВНУТРИ одного домена (us.checkout→www) — НЕ блок
    v = gate_verdict("https://us.checkout.gymshark.com/collections/2-inch",
                     "https://www.gymshark.com/en-ca/collections/2-inch", 200)
    assert not v["redirected"]
    # tinytronics: языковой редирект / → /en — НЕ блок (гейт №1: Rodion, норма)
    assert not gate_verdict("https://www.tinytronics.nl/",
                            "https://www.tinytronics.nl/en", 200)["redirected"]
    # схлопывание непустого пути в корень того же домена — блок
    assert gate_verdict("https://x.com/deep/page", "https://x.com/", 200)["redirected"]
    # трейлинг-слэш и http→https — не блок
    assert not gate_verdict("http://x.com/a/", "https://x.com/a", 200)["redirected"]
    # C9 bombas: HTTP 404 = мёртвая страница
    v = gate_verdict("https://bombas.com/collections/200-Giving-Back-Page-Test",
                     "https://bombas.com/404", 404)
    assert v["http_error"]
    # смена пути НЕ в корень (bombas /pages/find-a-store → /find-a-store) — не блок
    assert not gate_verdict("https://bombas.com/pages/find-a-store",
                            "https://bombas.com/find-a-store", 200)["redirected"]
    # нет финального URL (навигация умерла) — без вердикта, не падаем
    v = gate_verdict("https://x.com/a", "", None)
    assert not v["redirected"] and not v["http_error"]


def test_gated_result_shape():
    # минимальный результат шлюза совместим со step2/eval (path из url, пустая детекция)
    from scanners.base_scanner import gated_result
    r = gated_result("https://fritz-kola.de/cart", "checkout",
                     {"redirected": True, "final_url": "https://fritz-kola.com/",
                      "http_status": 200, "http_error": False, "errors": []})
    assert r["path"] == "/cart" and r["pixel_events"] == {} and r["has_cta"] is False
    assert r["gate"]["redirected"]


# ─── Hotjar: слияние без слабого hj\( (он матчил GTM-рантайм) ────────────────

def test_hotjar_merged_signatures():
    sigs = PLATFORM_SIGNATURES["Hotjar"]
    old_dead = "window.hjid = 5231524;"                   # признак затёртой строки жив
    survivor = "window.hjSetting = {}"                    # признак выжившей строки жив
    assert any(re.search(s, old_dead) for s in sigs)
    assert any(re.search(s, survivor) for s in sigs)


def test_hotjar_not_matched_by_gtm_runtime():
    # живые контексты из контейнеров gymshark/allbirds/plurio (2026-07-13):
    # минифицированные функции hJ()/HJ() рантайма GTM — НЕ Hotjar
    sigs = PLATFORM_SIGNATURES["Hotjar"]
    runtime_samples = ['b.set("RecentOnScreen",""+hJ().toString());',
                       'l=CJ("gtm.historyChange");\nHJ(l);GJ(l);']
    for sample in runtime_samples:
        assert not any(re.search(s, sample, re.IGNORECASE) for s in sigs), sample


# ─── Consent-баннер "Okay" (plurio.ai, гейт 2026-07-22) ──────────────────────

def test_okay_in_accept_texts_but_not_bare_ok():
    # малый баннер-чип "We use cookies… [Okay]" (Framer) не закрывался —
    # "okay" не было в списке. Голое "ok" запрещено: partial match зацепит
    # кнопки вида "Book now", "OK, отмена" и пол-интернета заодно.
    from popup_handler import ACCEPT_ALL_TEXTS
    assert "okay" in ACCEPT_ALL_TEXTS
    assert "ok" not in ACCEPT_ALL_TEXTS


# ─── Жёлтый флаг: cart-класс событие на lead-gen странице (plurio, 2026-07-22) ─

def test_yellow_flag_addtocart_on_lead_form():
    # plurio.ai: BOOK A DEMO шлёт Meta:AddToCart на lead_form/homepage —
    # событие работает, но commerce-классом. Жёлтый флаг, статус OK не меняется.
    from clicker import _derive_yellow_flag, _derive_red_flag
    yf, reason = _derive_yellow_flag("lead_form", ["Meta:AddToCart"])
    assert yf and "Lead" in reason
    # GA4-вариант написания
    yf2, _ = _derive_yellow_flag("homepage", ["Google Analytics:add_to_cart"])
    assert yf2
    # красный при этом НЕ поднимается (AddToCart не purchase-класс)
    rf, _ = _derive_red_flag("lead_form", ["Meta:AddToCart"])
    assert not rf


def test_yellow_flag_not_on_commerce_pages():
    # на product/checkout AddToCart легитимен — никаких флагов
    from clicker import _derive_yellow_flag
    assert _derive_yellow_flag("product_detail", ["Meta:AddToCart"]) == (False, None)
    assert _derive_yellow_flag("checkout", ["Meta:AddToCart"]) == (False, None)


def test_purchase_stays_red_not_yellow():
    # Purchase на lead_form — по-прежнему красный (misconfiguration), не жёлтый
    from clicker import _derive_red_flag
    rf, reason = _derive_red_flag("lead_form", ["Meta:Purchase"])
    assert rf and "purchase" in reason.lower() or rf


# ─── FB: доменный якорь против одноимённых импостеров (plurio, 2026-07-22) ───

def _fb_listing_html(raw_ads):
    # минимальный Relay-JSON блок как в реальной выдаче Ads Library
    import json as _json
    doc = {"result": {"search_results_connection": {
        "count": len(raw_ads),
        "edges": [{"node": {"collated_results": raw_ads}}]}}}
    return f'<script type="application/json">{_json.dumps(doc)}</script>'


def _fb_raw_ad(lib_id, page_name, page_uri, link_url):
    return {"ad_archive_id": lib_id, "page_name": page_name, "is_active": True,
            "snapshot": {"page_name": page_name, "page_profile_uri": page_uri,
                         "link_url": link_url, "body": {"text": "x"}}}


def test_domain_anchor_filters_impostor():
    # кейс plurio.ai: два рекламодателя «Plurio» (fuzzy-имя = 1.0 у обоих).
    # Клиент ведёт объявления на plurio.ai, импостер — на instagram.com/plurioid.
    from fb_ads_scraper import _extract_ads_from_json
    html = _fb_listing_html([
        _fb_raw_ad("111", "Plurio", "https://www.facebook.com/61563178984628/",
                   "https://www.plurio.ai/"),
        _fb_raw_ad("222", "Plurio", "https://www.facebook.com/plurioid/",
                   "https://www.instagram.com/plurioid"),
    ])
    out = _extract_ads_from_json(html, target_name="plurio", target_domain="plurio.ai")
    assert out["mode"] == "keyword_filtered_by_domain"
    assert [a["library_id"] for a in out["ads"]] == ["111"]
    assert out["brand_page_uris"] == ["https://www.facebook.com/61563178984628/"]


def test_domain_anchor_falls_back_to_name_when_no_match():
    # ни одно объявление не ведёт на домен (in-FB destinations) → прежний fuzzy
    from fb_ads_scraper import _extract_ads_from_json
    html = _fb_listing_html([
        _fb_raw_ad("333", "Plurio", "https://www.facebook.com/61563178984628/",
                   "http://fb.me/"),
    ])
    out = _extract_ads_from_json(html, target_name="plurio", target_domain="plurio.ai")
    assert out["mode"] == "keyword_filtered_by_name"
    assert [a["library_id"] for a in out["ads"]] == ["333"]


def test_ad_leads_to_domain_hosts():
    from fb_ads_scraper import _ad_leads_to_domain
    assert _ad_leads_to_domain({"link_url": "https://www.plurio.ai/", "caption": ""}, "plurio.ai")
    assert _ad_leads_to_domain({"link_url": "", "caption": "shop.plurio.ai"}, "plurio.ai")
    # чужой хост и хост-«матрёшка» не проходят
    assert not _ad_leads_to_domain({"link_url": "https://www.instagram.com/plurioid"}, "plurio.ai")
    assert not _ad_leads_to_domain({"link_url": "https://plurio.ai.evil.com/"}, "plurio.ai")


# ─── Form-fill journey: подбор значений и lead-класс (2026-07-22) ────────────

def test_form_fill_value_selection():
    from clicker import _value_for_field, TEST_FORM_VALUES
    assert _value_for_field({"kind": "email"}) == TEST_FORM_VALUES["email"]
    assert _value_for_field({"kind": "text", "placeholder": "Work email"}) == TEST_FORM_VALUES["email"]
    assert _value_for_field({"kind": "tel"}) == TEST_FORM_VALUES["tel"]
    assert _value_for_field({"kind": "text", "name": "phone_number"}) == TEST_FORM_VALUES["tel"]
    assert _value_for_field({"kind": "text", "name": "first_name"}) == TEST_FORM_VALUES["text"]


def test_lead_class_detection():
    from clicker import _has_lead_class, _lead_class_tags
    assert _has_lead_class(["Meta:Lead"])
    assert _has_lead_class(["Google Analytics:generate_lead"])
    assert _has_lead_class(["Meta:Contact"])
    assert _has_lead_class(["Meta:CompleteRegistration"])
    # плюрио-кейс: AddToCart после отправки PII — это НЕ lead-класс
    assert not _has_lead_class(["Meta:AddToCart", "Meta:SubscribedButtonClick"])
    # plumbing-события формы — не конверсия (давали ложное «Lead есть»)
    assert not _has_lead_class(["Google Analytics:gtm.formSubmit",
                                "Google Analytics:framer_form_submit"])
    # кастомный dataLayer-маркер ловится тегом
    assert _lead_class_tags(["Google Analytics:elly_lead"]) == ["Google Analytics:elly_lead"]


def test_ad_pixel_lead_separated_from_datalayer():
    # плюрио-урок: elly_lead в dataLayer есть, Meta:Lead нет — вердикты раздельны.
    # «Пиксель» в терминах Rodion'а = Meta/TikTok (сверяемо Pixel Helper'ом).
    from clicker import _lead_class_tags, _AD_PIXEL_PLATFORMS
    fired = ["Meta:AddToCart", "Google Analytics:elly_lead", "Meta:page_view_demo"]
    tags = _lead_class_tags(fired)
    assert tags == ["Google Analytics:elly_lead"]
    assert not any(t.split(":", 1)[0] in _AD_PIXEL_PLATFORMS for t in tags)
    # а вот Meta:Lead — пиксельный
    assert any(t.split(":", 1)[0] in _AD_PIXEL_PLATFORMS
               for t in _lead_class_tags(["Meta:Lead"]))


def test_payment_fields_never_filled():
    # политика: пароли и платёжные поля не заполняем
    from clicker import _FIELD_BLOCKLIST_RE
    for bad in ("card_number", "cvc", "cvv2", "expiry", "iban", "password"):
        assert _FIELD_BLOCKLIST_RE.search(bad), bad
    for ok in ("first_name", "work_email", "phone", "company"):
        assert not _FIELD_BLOCKLIST_RE.search(ok), ok
