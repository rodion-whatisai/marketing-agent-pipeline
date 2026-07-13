"""
Пин-тесты witness_check — логика ПОДТВЕРЖДЕНО / НЕ ЗАСВИДЕТЕЛЬСТВОВАНО / ПРОТИВОРЕЧИЕ.
Офлайн: синтетические witness-записи + (если лежит) реальный diag-JSON artbouquet.

Запуск: cd 01-client-discovery/engine && python -m pytest test_witness_check.py -q
"""

import json
from pathlib import Path

import pytest

import witness_check as wc
import eval_lib as ev

DIAG = Path(__file__).parent / "scans" / "_diag_meta_post_2026-07-13" / "artbouquet.shop_headless.json"


def _witness(pages, consent_clicked="text:accept"):
    return {"schema_version": 1, "mode": "pages", "date": "2026-07-13",
            "consent": {"clicked": consent_clicked, "on_page": "/"}, "pages": pages}


def _page(path="/", pixel_requests=None, hosts=None, status=200, redirected=False):
    return {"path": path, "http_status": status, "redirected": redirected,
            "final_url": f"https://x.com{path}", "pixel_requests": pixel_requests or [],
            "clickables": [], "third_party_hosts": hosts or []}


def test_platform_confirmed_by_page_hit():
    w = _witness([_page("/", pixel_requests=[
        {"platform": "Google Analytics", "method": "POST",
         "url": "https://www.google.com/ccm/collect?en=page_view", "query_event": "page_view",
         "body_events": []}])])
    expected = {"pages": {"/": {"platforms_detected": ["Google Analytics"]}}}
    (f,) = wc.check_domain(expected, [w], [])
    assert f["level"] == wc.OK and f["fact"] == "platform:Google Analytics"


def test_forbidden_violation_is_contradiction():
    # эталон запрещает Meta, а свидетель видел живой запрос к facebook.com/tr
    w = _witness([_page("/", pixel_requests=[
        {"platform": "Meta", "method": "POST", "url": "https://www.facebook.com/tr/",
         "query_event": "", "body_events": ["PageView"]}])])
    expected = {"pages": {"/": {"platforms_forbidden": ["Meta"]}}}
    (f,) = wc.check_domain(expected, [w], [])
    assert f["level"] == wc.CONTRADICTION


def test_forbidden_without_consent_is_unwitnessed():
    # consent не кликнут → отсутствие трафика ничего не доказывает
    w = _witness([_page("/")], consent_clicked=None)
    expected = {"pages": {"/": {"platforms_forbidden": ["Meta"]}}}
    (f,) = wc.check_domain(expected, [w], [])
    assert f["level"] == wc.UNWITNESSED and "consent" in f["detail"]


def test_meta_presence_requires_tr_not_sdk():
    # только загрузка SDK (connect.facebook.net) — скрипт лежит, но не стреляет:
    # присутствие Meta НЕ подтверждено
    w = _witness([_page("/", pixel_requests=[
        {"platform": "Meta", "method": "GET",
         "url": "https://connect.facebook.net/en_US/fbevents.js",
         "query_event": "", "body_events": []}])])
    expected = {"pages": {"/": {"platforms_detected": ["Meta"]}}}
    (f,) = wc.check_domain(expected, [w], [])
    assert f["level"] == wc.UNWITNESSED


def test_service_host_match_and_unknown_service():
    w = _witness([_page("/", hosts=["dfp.calendly.com", "cdn.example.com"])])
    expected = {"pages": {"/": {"external_services": ["Calendly", "НеведомыйСервис"]}}}
    by_fact = {f["fact"]: f for f in wc.check_domain(expected, [w], [])}
    assert by_fact["service:Calendly"]["level"] == wc.OK
    assert by_fact["service:НеведомыйСервис"]["level"] == wc.UNWITNESSED


def test_http_error_vs_ok_status_is_contradiction():
    w = _witness([_page("/dead", status=404)])
    expected = {"pages": {"/dead": {"status": "OK"}}}
    findings = wc.check_domain(expected, [w], [])
    assert any(f["fact"] == "http_status" and f["level"] == wc.CONTRADICTION for f in findings)


def test_redirect_flagged_for_gate():
    # кейс tinytronics: / → /en при эталонном OK — предупреждение на гейт, не противоречие
    w = _witness([_page("/", redirected=True)])
    expected = {"pages": {"/": {"status": "OK"}}}
    findings = wc.check_domain(expected, [w], [])
    (f,) = [x for x in findings if x["fact"] == "redirect"]
    assert f["level"] == wc.UNWITNESSED


@pytest.mark.skipif(not DIAG.exists(), reason="diag-JSON не на диске (gitignored сырьё)")
def test_event_found_in_real_journey_diag():
    # реальные journey-улики POST-слепоты: Meta:AddToCart в body_events (artbouquet)
    journey = json.loads(DIAG.read_text(encoding="utf-8"))
    expected = {"pages": {"/": {"conversion_events_min": ["Meta:AddToCart"]}}}
    findings = wc.check_domain(expected, [], [journey])
    (f,) = [x for x in findings if x["fact"] == "event:Meta:AddToCart"]
    assert f["level"] == wc.OK and "journey" in f["detail"]
