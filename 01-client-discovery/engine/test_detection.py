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


# ─── B6: Snapchat в GTM-сигнатурах ───────────────────────────────────────────

def test_snapchat_gtm_signature_matches_live_container_js():
    # кейс gymshark: живые snaptr-теги в контейнере не репортились
    sigs = PLATFORM_SIGNATURES["Snapchat Pixel"]
    container_js = "snaptr('track','ADD_CART');loadScript('https://sc-static.net/scevent.min.js')"
    assert any(re.search(s, container_js) for s in sigs)


# ─── Hotjar: обе группы признаков живы после слияния ─────────────────────────

def test_hotjar_merged_signatures():
    sigs = PLATFORM_SIGNATURES["Hotjar"]
    old_dead = "window.hjid = 5231524; hj('identify')"   # признаки затёртой строки
    survivor = "window.hjSetting = {}"                    # признаки выжившей строки
    assert any(re.search(s, old_dead) for s in sigs)
    assert any(re.search(s, survivor) for s in sigs)
