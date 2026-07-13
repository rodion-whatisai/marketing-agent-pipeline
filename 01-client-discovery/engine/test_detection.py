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
    import generate_report_html as grh
    assert grh.is_noise("Pinterest", "pagevisit")
    assert grh.is_noise("Meta", "PageView")
    assert not grh.is_noise("Pinterest", "checkout")


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
