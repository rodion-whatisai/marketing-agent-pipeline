"""
TNC Pipeline — Shared Utilities
================================
Единое место для общего кода. Импортируй отсюда — не копируй в каждый файл.
"""

import os
import sys
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from log import log_debug, log_warn

# ─── SSL: не проверяем сертификаты клиентских сайтов ─────────────────────────
# Сканер аудирует чужие сайты — битый/просроченный сертификат клиента не должен
# ронять скан (аналог wget --no-check-certificate). Мы ничего не отправляем,
# только читаем публичные страницы. Вернуть строгий режим: TNC_SSL_VERIFY=1.
# Патч кроет ВСЕ requests.get/post во всех модулях (каждый вызов идёт через
# Session.request); явный verify= в конкретном вызове остаётся уважаем.
# Playwright-контексты клиентских сайтов получают ignore_https_errors отдельно.
# Tested: 2026-07-08 on expired/self-signed/wrong.host.badssl.com — все три HTTP 200;
#         TNC_SSL_VERIFY=1 возвращает строгий режим (expired снова падает); google.com OK.
if os.environ.get("TNC_SSL_VERIFY") != "1":
    import requests as _requests
    import urllib3 as _urllib3
    _urllib3.disable_warnings(_urllib3.exceptions.InsecureRequestWarning)
    if not getattr(_requests.Session.request, "_tnc_no_verify", False):
        _orig_session_request = _requests.Session.request

        def _no_verify_request(self, *args, **kwargs):
            kwargs.setdefault("verify", False)
            return _orig_session_request(self, *args, **kwargs)

        _no_verify_request._tnc_no_verify = True
        _requests.Session.request = _no_verify_request

# ─── Paths ────────────────────────────────────────────────────────────────────

SCANS_DIR = Path(__file__).parent / "scans"


def _extract_domain(domain_or_url: str) -> str:
    """Вытаскивает чистый домен из URL или строки."""
    s = domain_or_url.strip()
    if "://" in s:
        return urlparse(s).netloc
    return s.replace("https://", "").replace("http://", "").split("/")[0]


def get_scan_dir(domain_or_url: str) -> Path:
    """Возвращает путь к папке scans/[domain]/, создаёт если нет."""
    path = SCANS_DIR / _extract_domain(domain_or_url)
    path.mkdir(parents=True, exist_ok=True)
    return path


def scan_path(domain_or_url: str, filename: str) -> Path:
    """Возвращает полный путь к файлу в scans/[domain]/."""
    return get_scan_dir(domain_or_url) / filename


def normalize_url(domain_or_url: str) -> str:
    """Возвращает нормализованный base URL с https://"""
    s = domain_or_url.strip().rstrip("/")
    if not s.startswith("http"):
        s = "https://" + s
    return s


# ─── Секреты из .env ──────────────────────────────────────────────────────────

def load_env(path=None) -> None:
    """Подгружает KEY=VALUE из .env рядом с движком в os.environ.

    setdefault — реальное окружение всегда приоритетнее файла. Идемпотентна,
    файла нет — тихо выходим. Нужна потому что ANTHROPIC_API_KEY в
    Windows-окружении регулярно терялся (set вместо setx, новые сессии/агенты) —
    движок не должен зависеть от того, кто и откуда его запустил.
    Формат: KEY=VALUE построчно, # — комментарий, кавычки вокруг значения опциональны.
    # Tested: 2026-07-09 — ключ из файла грузится, кавычки снимаются, пустые значения
    #         пропускаются, реальный env приоритетнее файла, отсутствие файла — тихий выход.
    """
    env_path = Path(path) if path else Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value:
            os.environ.setdefault(key, value)


# ─── Console encoding (Windows cp1252 fix) ────────────────────────────────────

# ANSI escape codes (цвета из log.py). Вырезаем при записи в файл — лог .txt чистый.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def setup_console() -> None:
    """Reconfigure stdout/stderr to UTF-8 if not already + включить ANSI/VT на Windows.
    Idempotent — повторный вызов безопасен. Фиксит UnicodeEncodeError на Windows
    (cp1252) для emoji / box-drawing символов в print(), и включает рендер цветов из log.py.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                if (getattr(stream, "encoding", "") or "").lower() != "utf-8":
                    stream.reconfigure(encoding="utf-8")
            except Exception:
                pass
    # Включаем ANSI/VT на Windows, чтобы цвета из log.py рендерились, а не печатались литералом.
    # Зовётся ДО подмены sys.stdout на TeeLogger (см. setup_logging) → colorama оборачивает
    # реальный stdout, а TeeLogger потом его захватывает в self.terminal.
    try:
        import colorama
        colorama.just_fix_windows_console()
    except Exception:
        pass


# ─── Logging ──────────────────────────────────────────────────────────────────

class TeeLogger:
    """Пишет stdout одновременно в терминал и в файл.
    В файл добавляет таймстамп [HH:MM:SS] к каждой непустой строке.
    В терминал пишет как есть — чтобы не ломать форматирование.
    """

    def __init__(self, log_path: str | Path):
        import datetime
        self.terminal = sys.stdout
        self.datetime = datetime
        log_path = Path(log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log = open(log_path, "a", encoding="utf-8")
        self._buf = ""  # буфер для сборки строки

    def write(self, msg):
        # Собираем в буфер, пишем построчно с таймстампом — и в терминал, и в файл
        self._buf += msg
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                ts = self.datetime.datetime.now().strftime("%H:%M:%S")
                term_out = f"[{ts}] {line}\n"                        # терминал — с цветом
                file_out = f"[{ts}] {_ANSI_RE.sub('', line)}\n"      # файл — без ANSI
            else:
                term_out = file_out = "\n"
            self.terminal.write(term_out)
            self.log.write(file_out)
        # Сразу флашим — не держим в буфере ОС
        self.terminal.flush()
        self.log.flush()

    def flush(self):
        self.terminal.flush()
        self.log.flush()

    def isatty(self):
        return False

    def close(self):
        # Дописываем остаток буфера если есть (в файл — без ANSI)
        if self._buf.strip():
            ts = self.datetime.datetime.now().strftime("%H:%M:%S")
            self.log.write(f"[{ts}] {_ANSI_RE.sub('', self._buf)}\n")
        self.log.close()


def setup_logging(domain: str, step: str = "step1") -> Path:
    """Пишет лог каждого шага в отдельный файл: step1_log.txt, step2_log.txt, report_log.txt."""
    setup_console()  # фикс cp1252 ДО захвата sys.stdout в TeeLogger
    d = _extract_domain(domain)
    log_path = scan_path(domain, f"{d}_{step}_log.txt")
    sys.stdout = TeeLogger(log_path)
    return log_path


def merge_logs(domain: str) -> Path:
    """Склеивает все step логи в один audit_log.txt. Возвращает путь."""
    d = _extract_domain(domain)
    steps = ["step1", "step2", "report"]
    merged_path = scan_path(domain, f"{d}_audit_log.txt")
    sep = chr(9552) * 65
    with open(merged_path, "w", encoding="utf-8") as out:
        for step in steps:
            log_path = scan_path(domain, f"{d}_{step}_log.txt")
            if log_path.exists():
                out.write(f"\n{sep}\n")
                out.write(f"  {step.upper()} LOG\n")
                out.write(f"{sep}\n\n")
                out.write(log_path.read_text(encoding="utf-8", errors="ignore"))
    return merged_path


# ─── HTTP ─────────────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def safe_get(url: str, timeout: int = 10, **kwargs):
    """requests.get с дефолтными headers. Возвращает Response или None."""
    import requests
    try:
        return requests.get(url, headers=HEADERS, timeout=timeout, **kwargs)
    except Exception:
        return None


# ─── Вежливые запросы: единое правило движка ─────────────────────────────────
#
# ПРАВИЛО (утв. Родионом 2026-07-20).
# Пускают нас на сайт или нет — решается ОДИН раз, на входе, по главной странице.
# Дальше это решение не пересматривается. Любой упавший запрос ПОСЛЕ этого —
# про наш темп и наш инструмент, а не про сайт:
#   429 = мы частим            → пауза (по Retry-After, если сервер его прислал) и ОДИН повтор
#   403 = нас приняли за бота  → повтор настоящим браузером (Playwright ходит как Chrome)
#   не вышло после этого       → честно «не дочитали». Слово «WAF» не употребляется.
# Ни один упавший запрос не имеет права породить вердикт о сайте.
#
# Правило действует ТОЛЬКО на аудируемый сайт клиента. Запросы к чужим сервисам
# (FB CDN с картинками, Ads Library, googletagmanager, Anthropic API) исключены
# списком NEVER_POLITE_HOSTS: там 429 лечится темпом на своём уровне, а браузерный
# повтор либо бесполезен, либо вреден (см. fb_audience_report — 273 картинки за прогон).

POLITE_TIMEOUT = 12          # секунд на requests-заход по сайту клиента
POLITE_PAUSE_SEC = 2.5       # пауза между запросами к одному домену
RETRY_429_WAIT = 6           # пауза перед повтором на 429, если сервер не прислал Retry-After
RETRY_AFTER_CAP = 30         # потолок ожидания: Retry-After=3600 ждать не будем
BROWSER_TIMEOUT_MS = 20000   # goto в Playwright-повторе

# Хосты, к которым правило вежливости НЕ применяется (не сайт клиента).
# Совпадение по суффиксу домена: "scontent.fyhu2-1.fna.fbcdn.net" → "fbcdn.net".
NEVER_POLITE_HOSTS = (
    "facebook.com", "fbcdn.net", "fbsbx.com", "fb.com",
    "google.com", "googleapis.com", "googletagmanager.com", "gstatic.com",
    "anthropic.com",
)

# Подменяется в тестах, чтобы не спать по-настоящему. В проде — time.sleep.
_polite_sleep = time.sleep

# Глубина вложенности polite_get: пока он работает, глобальный патч 429 молчит,
# иначе на один 429 пришлось бы две паузы (патч + сама функция).
_polite_depth = 0


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def is_polite_host(url: str) -> bool:
    """True — это сайт клиента, правило вежливости применимо.
    False — чужой сервис (FB CDN, Google, Anthropic), у него свой темп."""
    host = _host_of(url)
    if not host:
        return False
    return not any(host == h or host.endswith("." + h) for h in NEVER_POLITE_HOSTS)


def retry_after_sec(response, default: int = RETRY_429_WAIT) -> int:
    """Сколько ждать перед повтором. Уважаем Retry-After сервера, но с потолком —
    иначе Retry-After: 3600 остановит прогон на час."""
    raw = ""
    try:
        # requests отдаёт регистронезависимый CaseInsensitiveDict, Playwright —
        # обычный dict с ключами в нижнем регистре. Проверяем оба написания.
        hdrs = response.headers or {}
        raw = hdrs.get("Retry-After") or hdrs.get("retry-after") or ""
    except Exception:
        raw = ""
    try:
        wait = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default
    if wait <= 0:
        return default
    return min(wait, RETRY_AFTER_CAP)


class PoliteResult:
    """Итог вежливого захода. status=-1 — ответа не было вообще (сеть/DNS/таймаут).
    Несёт всё, что нужно вызывающим: тело, финальный URL после редиректов,
    заголовки и кодировку (перекодировка главной в classify_post_hoc без них не живёт)."""

    __slots__ = ("status", "text", "final_url", "headers", "encoding",
                 "apparent_encoding", "method", "reason")

    def __init__(self, status: int, text: str = None, final_url: str = None,
                 headers: dict = None, encoding: str = None,
                 apparent_encoding: str = None, method: str = "requests",
                 reason: str = None):
        self.status = status
        self.text = text
        self.final_url = final_url
        self.headers = headers or {}
        self.encoding = encoding
        self.apparent_encoding = apparent_encoding
        self.method = method              # requests | browser | none
        self.reason = reason              # текст исключения, если ответа не было

    @property
    def ok(self) -> bool:
        return self.status == 200 and bool(self.text)

    def __repr__(self) -> str:
        return f"<PoliteResult {self.status} {self.method} len={len(self.text or '')}>"


def browser_get(url: str, timeout_ms: int = BROWSER_TIMEOUT_MS) -> PoliteResult:
    """Повтор настоящим браузером — ходит как Chrome, поэтому бот-детект его пропускает.
    ignore_https_errors=True: для requests сертификаты выключены глобально (см. верх файла),
    браузерный повтор без этого падал бы там, где requests проходил."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return PoliteResult(-1, method="none", reason="playwright не установлен")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale="en-US",
                    ignore_https_errors=True,
                )
                page = ctx.new_page()
                resp = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                status = resp.status if resp else -1
                html = page.content()
                final = page.url or url
                headers = {}
                try:
                    headers = dict(resp.headers) if resp else {}
                except Exception:
                    headers = {}
                return PoliteResult(status, html, final, headers, method="browser")
            finally:
                browser.close()
    except Exception as e:
        return PoliteResult(-1, method="none", reason=str(e)[:120])


def polite_get(url: str, session=None, timeout: int = POLITE_TIMEOUT,
               allow_browser: bool = True, headers: dict = None) -> PoliteResult:
    """Единственная реализация правила вежливости. Возвращает PoliteResult, никогда не бросает.

    429 → пауза и один повтор тем же способом; 403 или отсутствие ответа → повтор
    браузером; дальше не идём. allow_browser=False — только дешёвая часть (для мест,
    где браузер стоит дороже пользы: массовое скачивание, батчи).

    # Tested: 2026-07-21 test_polite_http.py — 429→пауза→200 (один повтор, не два),
    #         повторный 429 не зацикливается, Retry-After уважается с потолком 30с,
    #         403→браузер, чужие хосты (fbcdn/google/anthropic) правило не трогает.
    """
    global _polite_depth
    import requests as _rq

    getter = session.get if session is not None else _rq.get
    hdrs = headers or HEADERS
    polite = is_polite_host(url)

    _polite_depth += 1                    # глушим глобальный патч на время своей работы
    try:
        try:
            r = getter(url, headers=hdrs, timeout=timeout)
            if r.status_code == 429 and polite:
                wait = retry_after_sec(r)
                log_warn(f"429 — мы частим; пауза {wait}с и один повтор: {url}")
                _polite_sleep(wait)
                r = getter(url, headers=hdrs, timeout=timeout)
            status = r.status_code
            if status == 200:
                return PoliteResult(status, r.text, r.url, dict(r.headers),
                                    r.encoding, getattr(r, "apparent_encoding", None))
            reason = None
        except Exception as e:
            status, reason = -1, str(e)[:120]
            log_debug(f"polite_get: запрос не состоялся ({reason}): {url}")
    finally:
        _polite_depth -= 1

    if status in (403, -1) and polite and allow_browser:
        log_debug(f"polite_get: {status} — повтор браузером: {url}")
        res = browser_get(url)
        if res.status == 200 and res.text:
            return res
        # Браузер тоже не пустил — отдаём его код, если он был; иначе исходный.
        return PoliteResult(res.status if res.status != -1 else status,
                            method=res.method, reason=res.reason or reason)

    return PoliteResult(status, method="requests" if status != -1 else "none",
                        reason=reason)


def fetch_note(status: int, method: str = None, home_ok: bool = None) -> str:
    """Честная формулировка исхода для лога и отчёта. Слово «WAF» тут не появляется
    намеренно: 403/429 на отдельной странице — это про наш заход, а не про сайт."""
    if status == 200:
        return "прочитано браузером" if method == "browser" else "прочитано"
    if status == 429:
        note = "не дочитали: сайт просит сбавить темп (429), повтор не помог"
    elif status == 403:
        note = "не дочитали: страница не отдалась ни обычным запросом, ни браузером (403)"
    elif status == -1 or status is None:
        note = "не дочитали: соединение не состоялось"
    elif status == 404:
        note = "страницы нет (404)"
    else:
        note = f"не дочитали: HTTP {status}"
    if home_ok and status not in (200, 404):
        note += " — главная сайта открылась, значит это наш заход, а не защита сайта"
    return note


# ─── Глобальный ретрай на 429 ────────────────────────────────────────────────
# Дешёвая половина правила, раздаваемая всем ~24 местам движка, которые зовут
# requests.get напрямую (step1, gtm, language_detector, FB discovery). Патч кроет
# их все, потому что любой requests.get идёт через Session.request — тот же приём,
# что и SSL-патч наверху файла. Дорогая половина (браузерный повтор на 403) сюда
# НЕ входит: из Session.request нельзя честно вернуть Response, собранный браузером.
#
# Границы: только GET/HEAD (повтор POST мог бы отправить форму дважды), только
# сайт клиента (см. is_polite_host), только один повтор, выключатель TNC_POLITE_RETRY=0.
if os.environ.get("TNC_POLITE_RETRY") != "0":
    import requests as _requests_polite

    if not getattr(_requests_polite.Session.request, "_tnc_polite", False):
        _orig_request_for_polite = _requests_polite.Session.request

        def _polite_request(self, method, url, *args, **kwargs):
            resp = _orig_request_for_polite(self, method, url, *args, **kwargs)
            if _polite_depth:                       # polite_get сам разберётся
                return resp
            if getattr(resp, "status_code", None) != 429:
                return resp
            if str(method).upper() not in ("GET", "HEAD"):
                return resp
            if not is_polite_host(url):
                return resp
            wait = retry_after_sec(resp)
            log_warn(f"429 — мы частим; пауза {wait}с и один повтор: {url}")
            _polite_sleep(wait)
            return _orig_request_for_polite(self, method, url, *args, **kwargs)

        _polite_request._tnc_polite = True
        # Сторож SSL-патча смотрит только на ВНЕШНЮЮ обёртку Session.request. Встав
        # поверх него, мы обязаны пронести его флаг дальше — иначе повторный импорт
        # (importlib.reload) не увидит SSL-патч под нами и намотает второй слой,
        # а за ним третий: цепочка растёт до RecursionError. Флаг ставим честно —
        # только если SSL-патч действительно есть под нами (TNC_SSL_VERIFY=1 его снимает).
        if getattr(_orig_request_for_polite, "_tnc_no_verify", False):
            _polite_request._tnc_no_verify = True
        _requests_polite.Session.request = _polite_request


# ─── Site Language Detection ──────────────────────────────────────────────────

def detect_site_language(html: str, headers: dict = None) -> dict:
    lang = None
    source = None
    if headers:
        cl = headers.get("Content-Language", headers.get("content-language", ""))
        if cl:
            lang = cl.strip().split(",")[0].strip().split("-")[0].lower()
            source = "Content-Language header"
    if not lang:
        m = re.search(r'<html[^>]+lang=["\']([a-zA-Z]{2,3}(?:-[a-zA-Z]{2,4})?)["\']', html, re.IGNORECASE)
        if m:
            lang = m.group(1).lower().split("-")[0]
            source = "html lang attr"
    if not lang:
        lang = "unknown"
        source = "not detected"
    is_english = lang in ("en", "unknown")
    return {"lang": lang, "is_english": is_english, "source": source}
