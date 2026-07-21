"""
Пины единого правила вежливых запросов (utils.polite_get + глобальный ретрай 429)
================================================================================
Правило (утв. Родионом 2026-07-20): пускают нас на сайт или нет — решается один раз
на входе. Дальше 429 = мы частим (пауза + ОДИН повтор), 403 = нас приняли за бота
(повтор браузером), не вышло — честно «не дочитали», без слова «WAF».

Сети тут нет вообще: requests подменяется на уровне HTTPAdapter.send, браузерный
повтор — подменой utils.browser_get, паузы — подменой utils._polite_sleep.

Запуск:  cd 01-client-discovery/engine && python -m pytest test_polite_http.py -q
"""
import requests
import pytest

import utils
from utils import (PoliteResult, fetch_note, is_polite_host, polite_get,
                   retry_after_sec)

CLIENT = "https://example-client.com/about"


# ─── Инструменты подмены ──────────────────────────────────────────────────────

def _resp(status: int, url: str = CLIENT, headers: dict = None, body: str = "ok"):
    """Настоящий requests.Response, собранный руками — без сети и без raw-потока."""
    r = requests.Response()
    r.status_code = status
    r.url = url
    r.raw = None
    r._content = body.encode("utf-8")
    r.encoding = "utf-8"
    if headers:
        r.headers.update(headers)
    return r


class FakeGetter:
    """Заменяет session.get: отдаёт заранее заготовленные ответы по очереди."""

    def __init__(self, *statuses, headers=None, raise_on=()):
        self.statuses = list(statuses)
        self.headers = headers or {}
        self.raise_on = raise_on
        self.calls = []

    def __call__(self, url, **kwargs):
        n = len(self.calls)
        self.calls.append(url)
        if n in self.raise_on:
            raise requests.exceptions.ConnectionError("нет соединения")
        status = self.statuses[min(n, len(self.statuses) - 1)]
        return _resp(status, url, self.headers if status == 429 else None)


class FakeSession:
    def __init__(self, getter):
        self.get = getter


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Ни один тест не спит по-настоящему; при этом видно, сколько и чего ждали."""
    slept = []
    monkeypatch.setattr(utils, "_polite_sleep", lambda s: slept.append(s))
    return slept


# ─── Граница правила: кому вежливость положена ────────────────────────────────

@pytest.mark.parametrize("url", [
    "https://example-client.com/page",
    "https://shop.example-client.com/",
    "http://acronis.com/en/company",
])
def test_сайт_клиента_попадает_под_правило(url):
    assert is_polite_host(url) is True


@pytest.mark.parametrize("url", [
    "https://scontent.fyhu2-1.fna.fbcdn.net/v/t45/img.jpg",   # превью креативов, 273 за прогон
    "https://www.facebook.com/ads/library/?q=x",
    "https://graph.facebook.com/123",
    "https://www.googletagmanager.com/gtm.js?id=GTM-XXX",
    "https://api.anthropic.com/v1/messages",
    "https://adstransparency.google.com/advertiser/AR",
])
def test_чужие_сервисы_под_правило_не_попадают(url):
    """У них свой темп (pacing в fb_audience_report, свой ретрай у Anthropic).
    Браузерный повтор на FB CDN записал бы HTML вместо JPEG — этого быть не должно."""
    assert is_polite_host(url) is False


# ─── Retry-After ──────────────────────────────────────────────────────────────

def test_retry_after_отсутствует_берём_дефолт():
    assert retry_after_sec(_resp(429)) == utils.RETRY_429_WAIT


def test_retry_after_уважается():
    assert retry_after_sec(_resp(429, headers={"Retry-After": "10"})) == 10


def test_retry_after_ограничен_потолком():
    """Retry-After: 3600 не должен останавливать прогон на час."""
    assert retry_after_sec(_resp(429, headers={"Retry-After": "3600"})) == utils.RETRY_AFTER_CAP


@pytest.mark.parametrize("raw", ["", "Wed, 21 Oct 2026 07:28:00 GMT", "-5", "0", "мусор"])
def test_retry_after_мусор_не_ломает(raw):
    assert retry_after_sec(_resp(429, headers={"Retry-After": raw})) == utils.RETRY_429_WAIT


# ─── 429: пауза и ОДИН повтор ─────────────────────────────────────────────────

def test_429_пауза_и_один_повтор_даёт_200(no_real_sleep):
    """Кейс allbirds.com/pages/our-story: параллельным агентам 429, с паузой 200."""
    getter = FakeGetter(429, 200)
    res = polite_get(CLIENT, session=FakeSession(getter))
    assert res.ok and res.status == 200
    assert len(getter.calls) == 2, "повтор ровно один"
    assert no_real_sleep == [utils.RETRY_429_WAIT]


def test_429_дважды_не_зацикливается(no_real_sleep):
    getter = FakeGetter(429, 429)
    res = polite_get(CLIENT, session=FakeSession(getter))
    assert res.status == 429 and not res.ok
    assert len(getter.calls) == 2, "второй 429 не порождает третью попытку"
    assert len(no_real_sleep) == 1


def test_429_ждём_столько_сколько_попросил_сервер(no_real_sleep):
    getter = FakeGetter(429, 200, headers={"Retry-After": "12"})
    polite_get(CLIENT, session=FakeSession(getter))
    assert no_real_sleep == [12]


def test_429_на_чужом_хосте_не_ретраится(no_real_sleep):
    getter = FakeGetter(429, 200)
    res = polite_get("https://scontent.fyhu2-1.fna.fbcdn.net/img.jpg",
                     session=FakeSession(getter))
    assert res.status == 429
    assert len(getter.calls) == 1
    assert no_real_sleep == []


# ─── 403 и обрыв связи: повтор браузером ──────────────────────────────────────

def test_403_уходит_в_браузер(monkeypatch):
    """Кейс acronis.com/en/company: fetch-инструменту 403, нормальному заходу 200."""
    seen = []

    def fake_browser(url, timeout_ms=None):
        seen.append(url)
        return PoliteResult(200, "<html>текст</html>", url, method="browser")

    monkeypatch.setattr(utils, "browser_get", fake_browser)
    res = polite_get(CLIENT, session=FakeSession(FakeGetter(403)))
    assert res.ok and res.method == "browser"
    assert seen == [CLIENT]


def test_403_без_браузера_отдаёт_честный_403(monkeypatch):
    called = []
    monkeypatch.setattr(utils, "browser_get",
                        lambda url, timeout_ms=None: called.append(url))
    res = polite_get(CLIENT, session=FakeSession(FakeGetter(403)), allow_browser=False)
    assert res.status == 403 and not res.ok
    assert called == [], "дорогой браузер не поднимается, когда его запретили"


def test_обрыв_связи_тоже_пробует_браузер(monkeypatch):
    monkeypatch.setattr(utils, "browser_get", lambda url, timeout_ms=None:
                        PoliteResult(200, "<html>ок</html>", url, method="browser"))
    res = polite_get(CLIENT, session=FakeSession(FakeGetter(200, raise_on=(0,))))
    assert res.ok and res.method == "browser"


def test_браузер_тоже_не_пустил_отдаём_честный_отказ(monkeypatch):
    monkeypatch.setattr(utils, "browser_get", lambda url, timeout_ms=None:
                        PoliteResult(403, method="browser"))
    res = polite_get(CLIENT, session=FakeSession(FakeGetter(403)))
    assert not res.ok and res.status == 403


def test_403_на_чужом_хосте_браузер_не_поднимает(monkeypatch):
    """273 картинки за прогон client-a — 273 запуска Chromium недопустимы."""
    called = []
    monkeypatch.setattr(utils, "browser_get",
                        lambda url, timeout_ms=None: called.append(url))
    res = polite_get("https://scontent.f1.fna.fbcdn.net/img.jpg",
                     session=FakeSession(FakeGetter(403)))
    assert res.status == 403 and called == []


def test_polite_get_никогда_не_бросает():
    class Boom:
        def get(self, *a, **k):
            raise RuntimeError("что угодно")

    res = polite_get(CLIENT, session=Boom(), allow_browser=False)
    assert res.status == -1 and res.reason


def test_200_несёт_финальный_url_и_кодировку():
    """classify_post_hoc чинит мохибейк по r.encoding/apparent_encoding и знает
    алиасы доменов по r.url — кортеж (код, тело) эти поля терял."""
    res = polite_get(CLIENT, session=FakeSession(FakeGetter(200)))
    assert res.final_url == CLIENT
    assert res.encoding == "utf-8"
    assert isinstance(res.headers, dict)


# ─── Формулировки: слова «WAF» нет ────────────────────────────────────────────

@pytest.mark.parametrize("status", [200, 403, 429, 404, 500, -1, None])
def test_формулировка_никогда_не_говорит_waf(status):
    for home_ok in (True, False, None):
        note = fetch_note(status, method="requests", home_ok=home_ok)
        low = note.lower()
        assert "waf" not in low and "ваф" not in low
        assert "cloudflare" not in low and "заблокирован" not in low


def test_формулировка_при_живой_главной_называет_причину_нашей():
    note = fetch_note(403, home_ok=True)
    assert "не дочитали" in note and "главная сайта открылась" in note


def test_404_это_не_недочитали():
    """Отсутствующая страница — факт о сайте, а не про наш темп."""
    assert "не дочитали" not in fetch_note(404, home_ok=True)


# ─── Глобальный ретрай 429 поверх requests ────────────────────────────────────

def test_патч_поставлен():
    assert getattr(requests.Session.request, "_tnc_polite", False) is True


class _AdapterStub:
    """Подменяет транспорт requests целиком: сети нет, а весь стек Session
    (включая оба наших патча — SSL и вежливость) отрабатывает по-настоящему."""

    def __init__(self, *statuses):
        self.statuses = list(statuses)
        self.calls = []
        self.verify_seen = []

    def __call__(self, request, **kwargs):
        # Экземпляр класса, подставленный как HTTPAdapter.send, методом не связывается —
        # requests зовёт его как send(request, **kwargs), без self-адаптера.
        n = len(self.calls)
        self.calls.append(request.url)
        self.verify_seen.append(kwargs.get("verify"))
        status = self.statuses[min(n, len(self.statuses) - 1)]
        r = _resp(status, request.url, {"Retry-After": "7"} if status == 429 else None)
        r.request = request
        return r


def _install(monkeypatch, stub):
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", stub, raising=True)


def test_глобальный_патч_ретраит_429_на_сайте_клиента(monkeypatch, no_real_sleep):
    """Ради этого патч и существует: ~24 места движка зовут requests.get напрямую,
    и переписывать их все по одному не нужно."""
    stub = _AdapterStub(429, 200)
    _install(monkeypatch, stub)
    r = requests.get(CLIENT, timeout=5)
    assert r.status_code == 200
    assert len(stub.calls) == 2
    assert no_real_sleep == [7], "Retry-After сервера уважён"


def test_глобальный_патч_не_трогает_чужие_хосты(monkeypatch, no_real_sleep):
    stub = _AdapterStub(429, 200)
    _install(monkeypatch, stub)
    r = requests.get("https://scontent.f1.fna.fbcdn.net/img.jpg", timeout=5)
    assert r.status_code == 429
    assert len(stub.calls) == 1 and no_real_sleep == []


def test_глобальный_патч_не_повторяет_post(monkeypatch, no_real_sleep):
    """Повтор POST мог бы отправить форму/заявку дважды."""
    stub = _AdapterStub(429, 200)
    _install(monkeypatch, stub)
    r = requests.post(CLIENT, json={"a": 1}, timeout=5)
    assert r.status_code == 429
    assert len(stub.calls) == 1 and no_real_sleep == []


def test_глобальный_патч_не_дублирует_паузу_внутри_polite_get(monkeypatch, no_real_sleep):
    """polite_get сам делает паузу; патч на время его работы обязан молчать,
    иначе на один 429 придётся две паузы и два лишних запроса."""
    stub = _AdapterStub(429, 200)
    _install(monkeypatch, stub)
    res = polite_get(CLIENT, session=requests.Session())
    assert res.ok
    assert len(stub.calls) == 2, "ровно один повтор, а не два"
    assert len(no_real_sleep) == 1


# ─── step2: 403/429 — это наш заход, а не мёртвая страница ───────────────────

class _FakeResponse:
    def __init__(self, status, headers=None):
        self.status = status
        self.headers = headers or {}


class _FakePage:
    """Минимальный двойник Playwright-страницы: считает goto и паузы."""

    def __init__(self, *statuses, headers=None):
        self.statuses = list(statuses)
        self.headers = headers or {}
        self.gotos = []
        self.waits = []
        self.url = CLIENT

    def goto(self, url, **kwargs):
        n = len(self.gotos)
        self.gotos.append(url)
        status = self.statuses[min(n, len(self.statuses) - 1)]
        return _FakeResponse(status, self.headers if status == 429 else None)

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


def test_429_на_странице_даёт_паузу_и_повтор():
    """Реальный корпус: страница с 429 уходила в «мертва» без единой попытки."""
    from scanners.base_scanner import navigate_and_gate
    page = _FakePage(429, 200, headers={"retry-after": "9"})   # Playwright: ключи в нижнем регистре
    verdict = navigate_and_gate(page, CLIENT)
    assert len(page.gotos) == 2, "повтор ровно один"
    assert 9000 in page.waits, "пауза взята из Retry-After сервера"
    assert verdict["http_status"] == 200
    assert verdict["http_error"] is False


def test_403_на_странице_тоже_повторяется():
    from scanners.base_scanner import navigate_and_gate
    page = _FakePage(403, 200)
    verdict = navigate_and_gate(page, CLIENT)
    assert len(page.gotos) == 2
    assert verdict["http_error"] is False


def test_повтор_не_помог_отдаём_честный_код():
    from scanners.base_scanner import navigate_and_gate
    page = _FakePage(429, 429)
    verdict = navigate_and_gate(page, CLIENT)
    assert len(page.gotos) == 2, "второй 429 не порождает третью попытку"
    assert verdict["http_status"] == 429 and verdict["http_error"] is True


def test_404_не_повторяем():
    """Отсутствующая страница — факт о сайте, повторять нечего."""
    from scanners.base_scanner import navigate_and_gate
    page = _FakePage(404)
    verdict = navigate_and_gate(page, CLIENT)
    assert len(page.gotos) == 1
    assert verdict["http_error"] is True


def test_200_не_повторяем():
    from scanners.base_scanner import navigate_and_gate
    page = _FakePage(200)
    assert len(navigate_and_gate(page, CLIENT)) and len(page.gotos) == 1


def test_ssl_патч_пережил_соседа(monkeypatch, no_real_sleep):
    """Вежливость встала поверх SSL-патча — verify=False должен доезжать до транспорта."""
    stub = _AdapterStub(200)
    _install(monkeypatch, stub)
    requests.get(CLIENT, timeout=5)
    assert stub.verify_seen == [False]


def test_повторный_импорт_не_наматывает_второй_слой(monkeypatch):
    """importlib.reload(utils) переисполняет ОБА патча. Если сторожа не видят друг
    друга, цепочка обёрток растёт на каждый reload и упирается в RecursionError.
    Пин поведенческий: после reload один 429 по-прежнему даёт РОВНО один повтор."""
    import importlib
    importlib.reload(utils)                       # reload сбрасывает _polite_sleep — чиним после
    slept = []
    monkeypatch.setattr(utils, "_polite_sleep", lambda s: slept.append(s))

    stub = _AdapterStub(429, 200)
    _install(monkeypatch, stub)
    r = requests.get(CLIENT, timeout=5)

    assert r.status_code == 200
    assert len(stub.calls) == 2, "слоёв патча больше одного — цепочка намоталась"
    assert len(slept) == 1
