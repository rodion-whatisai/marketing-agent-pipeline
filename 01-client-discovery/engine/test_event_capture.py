"""
Пин-тесты чтения имени события из POST-тела запроса (BUGS-2026-07-13, Проблема 1).

Meta шлёт содержательные события multipart/urlencoded POST'ом (name="ev"),
TikTok — JSON-телом ("event":..). До фикса get_event_from_url читал только query →
события деградировали в 'fired' → системный ложный GAP. get_event_from_request
дочитывает тело, НО только когда query пуст (GET-пиксели типа GA4 не трогаются).

Запуск: cd 01-client-discovery/engine && python -m pytest test_event_capture.py -q
"""

from collections import Counter
from pathlib import Path
import pytest

from scanners.base_scanner import get_event_from_request, is_conversion_event

FIX = Path(__file__).parent / "scans" / "_fixtures" / "post_bodies"

# URL'ы БЕЗ имени события в query → get_event_from_url вернёт 'fired' → читаем тело
META_URL = "https://www.facebook.com/tr/"
TT_URL = "https://analytics.tiktok.com/api/v2/pixel"
GA_URL = "https://region1.google-analytics.com/g/collect?v=2&en=add_to_cart"
SNAP_URL = "https://tr.snapchat.com/gtm/v2"


class _FakeRequest:
    """Минимальный request: url + post_data как обычный атрибут."""
    def __init__(self, url, post_data=None):
        self.url = url
        self.post_data = post_data


class _SpyRequest:
    """post_data как property — ставит флаг read при обращении.
    Доказывает, что тело НЕ читается, когда имя нашлось в query (пин)."""
    def __init__(self, url, body):
        self.url = url
        self._body = body
        self.read = False

    @property
    def post_data(self):
        self.read = True
        return self._body


# ─── Реальные формы тел (сепаратор \r\r\n — как в живых фикстурах Meta) ───────

META_MP_ATC = (
    '------WebKitFormBoundaryX\r\r\n'
    'Content-Disposition: form-data; name="id"\r\r\n\r\r\n'
    '3546919788950632\r\r\n'
    '------WebKitFormBoundaryX\r\r\n'
    'Content-Disposition: form-data; name="ev"\r\r\n\r\r\n'
    'AddToCart\r\r\n'
    '------WebKitFormBoundaryX--\r\r\n'
)

META_MP_VC = (
    '------B\r\r\n'
    'Content-Disposition: form-data; name="ev"\r\r\n\r\r\n'
    'ViewContent\r\r\n'
    '------B--\r\r\n'
)

META_MP_SB = (
    '------B\r\r\n'
    'Content-Disposition: form-data; name="ev"\r\r\n\r\r\n'
    'SubscribedButtonClick\r\r\n'
    '------B--\r\r\n'
)

# поля se/ss с JSON-значениями ИДУТ ПЕРЕД ev — не должны сбить парсер
META_MP_SE_BEFORE_EV = (
    '------B\r\r\n'
    'Content-Disposition: form-data; name="se"\r\r\n\r\r\n'
    '{"page_url":true,"referrer":true,"fbp":true}\r\r\n'
    '------B\r\r\n'
    'Content-Disposition: form-data; name="ss"\r\r\n\r\r\n'
    '{"page_title":[{"rid":"1","v":["X"]}]}\r\r\n'
    '------B\r\r\n'
    'Content-Disposition: form-data; name="ev"\r\r\n\r\r\n'
    'AddToCart\r\r\n'
    '------B--\r\r\n'
)

META_URLENC = "id=354&ev=PageView&dl=https%3A%2F%2Fx.com&rqm=formPOST"

TT_ATC = '{"event":"AddToCart","event_id":"sh-1","timestamp":"2026-07-13T04:13:14.269Z"}'
TT_VC = '{"event":"ViewContent","event_id":"sh-2"}'
TT_PAGEVIEW = '{"event":"Pageview","event_id":"sh-3","is_onsite":false}'
# тело без ключа event — только служебные поля → честный 'fired'
TT_NO_EVENT = '{"event_id":"","action":"Pf","auto_collected_properties":{"page_trigger":"PageView"}}'


# ─── Meta: три формата, имя события достаётся ─────────────────────────────────

def test_meta_multipart_addtocart():
    assert get_event_from_request(_FakeRequest(META_URL, META_MP_ATC), "Meta") == "AddToCart"

def test_meta_multipart_crlf_variant():
    # \r\r\n сепаратор реальных фикстур покрыт [\r\n]+
    assert get_event_from_request(_FakeRequest(META_URL, META_MP_VC), "Meta") == "ViewContent"

def test_meta_multipart_subscribedbuttonclick_extracted_but_not_conversion():
    ev = get_event_from_request(_FakeRequest(META_URL, META_MP_SB), "Meta")
    assert ev == "SubscribedButtonClick"
    # авто-кнопка Shopify — НЕ конверсия, дыру не озеленяет
    assert is_conversion_event("Meta", ev) is False

def test_meta_multipart_ignores_se_ss_json_fields():
    # якорь name="ev" не цепляет JSON-значения полей se/ss/pmd, идущих раньше
    assert get_event_from_request(_FakeRequest(META_URL, META_MP_SE_BEFORE_EV), "Meta") == "AddToCart"

def test_meta_urlencoded():
    assert get_event_from_request(_FakeRequest(META_URL, META_URLENC), "Meta") == "PageView"


# ─── TikTok: JSON-тело ───────────────────────────────────────────────────────

def test_tiktok_json_addtocart():
    # берём ключ event, а не event_id
    assert get_event_from_request(_FakeRequest(TT_URL, TT_ATC), "TikTok") == "AddToCart"

def test_tiktok_json_viewcontent():
    assert get_event_from_request(_FakeRequest(TT_URL, TT_VC), "TikTok") == "ViewContent"

def test_tiktok_json_pageview():
    assert get_event_from_request(_FakeRequest(TT_URL, TT_PAGEVIEW), "TikTok") == "Pageview"

def test_tiktok_json_no_event_key_is_fired():
    # action / page_trigger — не ключ event → честный 'fired', ничего не выдумываем
    assert get_event_from_request(_FakeRequest(TT_URL, TT_NO_EVENT), "TikTok") == "fired"


# ─── ПИН: query выиграл → тело НЕ читаем (GET-пиксели без изменений) ──────────

def test_query_wins_body_untouched():
    spy = _SpyRequest("https://www.facebook.com/tr/?id=1&ev=PageView", META_MP_ATC)
    assert get_event_from_request(spy, "Meta") == "PageView"
    assert spy.read is False   # к телу не притронулись

def test_ga4_get_pixel_unchanged():
    # у GA4 имя (en=) в query даже при POST — тело не трогаем
    spy = _SpyRequest(GA_URL, '{"event":"purchase"}')
    assert get_event_from_request(spy, "Google Analytics") == "add_to_cart"
    assert spy.read is False

def test_snapchat_event_param_none_body_untouched():
    # у Snapchat event_param=None — имя события в теле не ищем
    spy = _SpyRequest(SNAP_URL, '{"event":"PURCHASE"}')
    assert get_event_from_request(spy, "Snapchat") == "fired"
    assert spy.read is False


# ─── Пустое тело и устойчивость к обрезке ────────────────────────────────────

def test_empty_body_is_fired():
    assert get_event_from_request(_FakeRequest(META_URL, None), "Meta") == "fired"
    assert get_event_from_request(_FakeRequest(META_URL, ""), "Meta") == "fired"

def test_truncated_json_still_extracts():
    # тело обрезано после значения (кап PIXEL_HIT_BODY_CAP / сеть) — имя в начале
    assert get_event_from_request(_FakeRequest(TT_URL, '{"event":"AddToCart","event_id":"sh-59a'), "TikTok") == "AddToCart"

def test_truncated_multipart_still_extracts():
    body = ('------B\r\r\nContent-Disposition: form-data; name="ev"\r\r\n\r\r\nAddToCart')
    assert get_event_from_request(_FakeRequest(META_URL, body), "Meta") == "AddToCart"


# ─── Прогон по всем 59 реальным письмам (scans/ в .gitignore → локально) ─────

@pytest.mark.skipif(not FIX.exists(), reason="фикстуры POST-тел в .gitignore — прогон только локально")
def test_sweep_all_post_body_fixtures():
    results = {"Meta": Counter(), "TikTok": Counter()}
    files = sorted(FIX.glob("*.txt"))
    assert files, "фикстуры есть, но *.txt не найдены"
    for f in files:
        platform = "Meta" if "_Meta_" in f.name else "TikTok" if "_TikTok_" in f.name else None
        if platform is None:
            continue
        url = META_URL if platform == "Meta" else TT_URL
        ev = get_event_from_request(_FakeRequest(url, f.read_text(encoding="utf-8")), platform)
        results[platform][ev] += 1
        # чистое извлечение: без переводов строк, кавычек, огрызков boundary
        assert "\n" not in ev and '"' not in ev and not ev.startswith("--"), (f.name, repr(ev))
    # у ВСЕХ Meta-тел есть ev → ни одного 'fired' (иначе парсер ослеп на формате)
    assert results["Meta"]["fired"] == 0, dict(results["Meta"])
    # ключевые конверсии реально извлекаются с живых сайтов
    assert results["Meta"]["AddToCart"] >= 1, dict(results["Meta"])
    assert results["TikTok"]["AddToCart"] >= 1, dict(results["TikTok"])
    # TikTok: только тела без ключа event дают 'fired' (служебные пинги)
    assert results["TikTok"]["fired"] >= 1, dict(results["TikTok"])
