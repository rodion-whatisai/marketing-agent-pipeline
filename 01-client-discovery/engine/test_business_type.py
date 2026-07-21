"""
Тесты классификатора типа бизнеса (лесенка v3) — без сети, на синтетике.
Фикстуры — те же, что в самотесте business_type_classifier.__main__:
здесь они бегут автоматически при каждом `pytest -q` (прекоммитный гейт PROCESS.md).

Эталоны по РЕАЛЬНЫМ доменам — отдельно, через гейт Родиона (golden/, после «да»).
"""
import pytest

from business_type_classifier import (
    _FIXTURE_AUDIENCE_B2B, _FIXTURE_B2B, _FIXTURE_B2C, _FIXTURE_EMPTY,
    _FIXTURE_GAME_STUDIO, _FIXTURE_MIXED, _FIXTURE_SUITE_BODY,
    _FIXTURE_TRIAL_SAAS, classify_business, needs_llm, parse_site_verdict,
    pick_about_urls,
)


# ── Лесенка: тип покупателя ──────────────────────────────────────────────────

@pytest.mark.parametrize("name,fixture,expected_buyer", [
    ("розничный магазин", _FIXTURE_B2C, "b2c"),
    ("b2b-кибербез", _FIXTURE_B2B, "b2b"),
    ("обе линии → mixed", _FIXTURE_MIXED, "mixed"),
    ("пустая страница", _FIXTURE_EMPTY, "unknown"),
    ("корп-сайт игровой студии", _FIXTURE_GAME_STUDIO, "b2c"),
    ("адресат for teams", _FIXTURE_AUDIENCE_B2B, "b2b"),
    ("free trial не тянет в b2c", _FIXTURE_TRIAL_SAAS, "b2b"),
])
def test_buyer_type(name, fixture, expected_buyer):
    result = classify_business(fixture, domain=name)
    assert result["buyer_type"] == expected_buyer, \
        f"{name}: {result['buyer_type']} через {result['decided_by']}"


def test_lone_button_not_high():
    """Одна кнопка «Request a demo» на пустой странице ≠ high-уверенность."""
    result = classify_business(
        "<h1>Acme Consulting</h1><p>We build things.</p>"
        "<button>Request a demo</button>", domain="lone-button")
    assert result["confidence"] != "high"


def test_observed_click_beats_dictionary():
    """Наблюдение кликера (AddToCart стрельнул) решает даже пустую страницу."""
    result = classify_business(_FIXTURE_EMPTY, domain="observed",
                               observed={"add_to_cart_fired": True})
    assert result["buyer_type"] == "b2c"
    assert "observed" in result["decided_by"]


def test_platform_prior_is_not_strong():
    """Shopify-prior — слабая улика: сама по себе не делает mixed и не даёт
    сильную B2C-сторону сайту с чистыми B2B-уликами."""
    result = classify_business(_FIXTURE_TRIAL_SAAS, domain="platform-weak",
                               platform="shopify")
    assert result["buyer_type"] == "b2b", \
        f"платформа перетянула: {result['buyer_type']} через {result['decided_by']}"


# ── Индустрия: самоописание, не тело ─────────────────────────────────────────

@pytest.mark.parametrize("name,fixture,expected_industry", [
    ("магазин обуви", _FIXTURE_B2C, "fashion"),
    ("кибербез из title", _FIXTURE_B2B, "cybersecurity"),
    ("студия игр из meta", _FIXTURE_GAME_STUDIO, "gaming"),
])
def test_industry_from_self_description(name, fixture, expected_industry):
    assert classify_business(fixture, domain=name)["industry"] == expected_industry


def test_body_words_do_not_fake_industry():
    """v1-регрессия: suite/garage/components в ТЕЛЕ не дают tourism/automotive —
    индустрия берётся из title (cybersecurity)."""
    result = classify_business(_FIXTURE_SUITE_BODY, domain="suite-body")
    assert result["industry"] == "cybersecurity"
    assert "travel" not in (result["industry_tags"] or [])
    assert "automotive" not in (result["industry_tags"] or [])


def test_coding_bootcamp_is_education_not_devtools():
    """tripleten-регрессия: «Online Coding Bootcamps» — образование, не девтулзы."""
    html = ('<html lang="en"><head><title>TT: Online Coding Bootcamps</title>'
            '<meta name="description" content="Get into tech from scratch."></head>'
            "<body></body></html>")
    result = classify_business(html, domain="bootcamp")
    assert result["industry"] == "education"


def test_fb_category_resolves_empty_page():
    """FB-категория решает индустрию при пустом самоописании."""
    html = "<html><head><title>Client-A</title></head><body></body></html>"
    result = classify_business(html, domain="fbcat", fb_category="Education website")
    assert result["industry"] == "education"


# ── Дочитка: фильтр payment-страниц ──────────────────────────────────────────

def test_payment_pages_excluded_from_about_fetch():
    """tripleten-регрессия: /about/payment-options (рассрочка клиента) не в дочитку."""
    classified = [
        {"url": "https://x.com/about/payment-options", "path": "/about/payment-options"},
        {"url": "https://x.com/about", "path": "/about"},
        {"url": "https://x.com/contacts", "path": "/contacts"},
    ]
    urls = pick_about_urls(classified)
    assert "https://x.com/about" in urls
    assert all("payment" not in u for u in urls)


# ── Haiku-обвязка (без сети) ─────────────────────────────────────────────────

def test_needs_llm_triggers():
    assert needs_llm({"buyer_type": "unknown", "confidence": "unknown",
                      "business_model": [], "industry": None}) == \
        ["buyer_weak", "model_empty", "industry_null"]
    assert needs_llm({"buyer_type": "b2c", "confidence": "high",
                      "business_model": ["ecom"], "industry": "fashion"}) == []


def test_parse_site_verdict_validates():
    """Enum-валидация + проверка цитат-подстрок (галлюцинации отбрасываются)."""
    material = "Website: x.com Title: Shoes for everyone"
    out = parse_site_verdict({
        "buyer_type": "B2C", "probability_b2c": 1.7,
        "business_model": ["ecom", "чушь"], "industry": "Fashion",
        "evidence": {"buyer": ["Shoes for everyone", "выдуманная цитата"]},
    }, material)
    assert out["buyer_type"] == "b2c"
    assert out["probability_b2c"] == 1.0          # клэмп
    assert out["business_model"] == ["ecom"]      # чушь отброшена
    assert out["evidence"]["buyer"] == ["Shoes for everyone"]
    assert out["evidence_verified"] is False      # выдуманная цитата зафиксирована

    assert parse_site_verdict(["не", "объект"], material) == {}
