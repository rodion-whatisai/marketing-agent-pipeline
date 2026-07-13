"""
Пин-тесты platforms.py — шаг A: ЭКВИВАЛЕНТНОСТЬ.
Каждый derived view обязан быть байт-в-байт равен замороженной копии старой
таблицы (test_platforms_frozen.py, снята 2026-07-13 ДО рефакторинга) — и по
значениям, и по ПОРЯДКУ ключей (make_listeners матчит первым совпавшим).

Запуск: cd 01-client-discovery/engine && python -m pytest test_platforms.py -q
"""

import pytest

import platforms as P
import test_platforms_frozen as F


def _assert_equal_with_order(view: dict, frozen: dict, frozen_order: list):
    assert view == frozen, "содержимое разошлось с замороженной таблицей"
    assert list(view.keys()) == frozen_order, "порядок ключей разошёлся"


def test_pixel_rules_equivalent():
    _assert_equal_with_order(P.as_pixel_rules(), F.FROZEN_PIXEL_RULES,
                             F.FROZEN_PIXEL_RULES_KEY_ORDER)


def test_tier1_equivalent():
    _assert_equal_with_order(P.as_conversion_tier1(), F.FROZEN_TIER1,
                             F.FROZEN_TIER1_KEY_ORDER)


def test_tier2_equivalent():
    _assert_equal_with_order(P.as_conversion_tier2(), F.FROZEN_TIER2,
                             F.FROZEN_TIER2_KEY_ORDER)


def test_noise_equivalent():
    _assert_equal_with_order(P.as_noise_events(), F.FROZEN_NOISE,
                             F.FROZEN_NOISE_KEY_ORDER)


def test_shopify_app_ids_equivalent():
    _assert_equal_with_order(P.as_shopify_pixel_platforms(), F.FROZEN_SHOPIFY_APP_IDS,
                             F.FROZEN_SHOPIFY_APP_IDS_KEY_ORDER)


def test_shopify_markers_equivalent():
    _assert_equal_with_order(P.as_shopify_pixel_markers(), F.FROZEN_SHOPIFY_MARKERS,
                             F.FROZEN_SHOPIFY_MARKERS_KEY_ORDER)


def test_gtm_signatures_equivalent():
    # включая эффективное значение Hotjar-дубля (вторая запись победила первую)
    # и позицию Hotjar между TikTok Pixel и Microsoft/Bing
    _assert_equal_with_order(P.as_gtm_platform_signatures(), F.FROZEN_GTM_SIGNATURES,
                             F.FROZEN_GTM_SIGNATURES_KEY_ORDER)


def test_duplicate_key_raises():
    # класс Hotjar-бага умирает конструктивно: дубликат = исключение, не тихая замена
    with pytest.raises(ValueError, match="дубликат"):
        P._build([("Hotjar", [1]), ("Hotjar", [2])])


def test_consumers_import_views():
    # потребители реально читают реестр, а не свои копии
    from scanners import base_scanner as b
    from scanners import shopify_scanner as s
    import gtm_analyzer as g
    assert b.PIXEL_RULES is not None and b.PIXEL_RULES == P.as_pixel_rules()
    assert b.CONVERSION_EVENTS_TIER1 == P.as_conversion_tier1()
    assert b.CONVERSION_EVENTS_TIER2 == P.as_conversion_tier2()
    assert b.NOISE_EVENTS == P.as_noise_events()
    assert s.SHOPIFY_PIXEL_PLATFORMS == P.as_shopify_pixel_platforms()
    assert s.SHOPIFY_PIXEL_MARKERS == P.as_shopify_pixel_markers()
    assert g.PLATFORM_SIGNATURES == P.as_gtm_platform_signatures()
