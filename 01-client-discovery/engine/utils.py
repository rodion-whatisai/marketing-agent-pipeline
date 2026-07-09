"""
TNC Pipeline — Shared Utilities
================================
Единое место для общего кода. Импортируй отсюда — не копируй в каждый файл.
"""

import os
import sys
import re
from pathlib import Path
from urllib.parse import urlparse

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
