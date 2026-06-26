"""
TNC Pipeline — Логирование с уровнями и цветами
================================================
Единый логгер для всего движка. Импортируй отсюда:

    from log import log_info, log_warn, log_error, log_debug, log_success, log_step

Уровни по возрастанию важности: FIRE < DEBUG < INFO < SUCCESS < WARN < ERROR.
Порог по умолчанию — DEBUG (видно почти всё, КРОМЕ FIRE). FIRE — построчный
firehose (по каждому URL/ссылке), включается отдельно: LOG_LEVEL=FIRE или --fire.
Приглушить: env LOG_LEVEL=INFO/WARN/ERROR, set_level("INFO") или флаг --quiet
у entry points. Всё что ниже порога — не печатается.

Цвет — только в терминал (рендерит colorama, включается в utils.setup_console).
В файл TeeLogger пишет ту же строку, но без ANSI (вырезает на своей стороне).
Тег [LEVEL] — обычный текст: попадает и в терминал, и в файл → грепается по [ERROR] и т.п.

Печатаем обычным print() — поток ловит TeeLogger (см. utils.setup_logging),
поэтому в каждом файле движка можно просто звать log_* вместо print.
"""

import os
import sys
import traceback

# ─── Уровни ─────────────────────────────────────────────────────────────────
# FIRE — глубже DEBUG: построчный firehose (по каждому URL/ссылке).
# По умолчанию НЕ виден (порог = DEBUG). Включить: LOG_LEVEL=FIRE или флаг --fire.
FIRE, DEBUG, INFO, SUCCESS, WARN, ERROR = 5, 10, 20, 25, 30, 40

_NAMES = {
    "FIRE": FIRE, "DEBUG": DEBUG, "INFO": INFO, "SUCCESS": SUCCESS,
    "WARN": WARN, "WARNING": WARN, "ERROR": ERROR,
}


def _initial_level() -> int:
    # Дефолт DEBUG — по умолчанию видно ВСЁ. Приглушить: LOG_LEVEL=INFO/WARN/ERROR или флаг --quiet.
    env = (os.environ.get("LOG_LEVEL") or "").strip().upper()
    return _NAMES.get(env, DEBUG)


_LEVEL = _initial_level()


def set_level(name) -> int:
    """Установить порог логирования. name: 'DEBUG'/'INFO'/'SUCCESS'/'WARN'/'ERROR' или число."""
    global _LEVEL
    if isinstance(name, int):
        _LEVEL = name
    else:
        _LEVEL = _NAMES.get(str(name).strip().upper(), INFO)
    return _LEVEL


def get_level() -> int:
    return _LEVEL


# ─── Цвета (ANSI) ─────────────────────────────────────────────────────────────
_RESET = "\033[0m"

_COLORS = {
    FIRE:    "\033[2;90m",    # ещё тусклее — построчный firehose
    DEBUG:   "\033[2;37m",    # тусклый серый — отладочный шум
    INFO:    "\033[36m",      # cyan
    SUCCESS: "\033[32m",      # green
    WARN:    "\033[33m",      # yellow
    ERROR:   "\033[1;31m",    # bold red
}
_STEP_COLOR   = "\033[1;35m"  # bold magenta — старт фазы
_HEADER_COLOR = "\033[1;36m"  # bold cyan — баннер раздела

# Текстовый тег + дефолтный эмодзи на уровень
_META = {
    FIRE:    ("FIRE",  "🔥"),
    DEBUG:   ("DEBUG", "🐛"),
    INFO:    ("INFO",  "•"),
    SUCCESS: ("OK",    "✅"),
    WARN:    ("WARN",  "⚠️"),
    ERROR:   ("ERROR", "❌"),
}


def _emit(level: int, msg, emoji=None) -> None:
    if level < _LEVEL:
        return
    tag, default_emoji = _META[level]
    ic = emoji if emoji is not None else default_emoji
    color = _COLORS.get(level, "")
    if level in (ERROR, WARN, DEBUG, FIRE):
        # критичное/отладочное — красим всю строку, чтобы не пропустить / явно приглушить
        line = f"{color}{ic} [{tag}] {msg}{_RESET}"
    else:
        # info/success — красим только тег, текст обычным цветом (читаемо при потоке)
        line = f"{ic} {color}[{tag}]{_RESET} {msg}"
    print(line)


# ─── Публичные функции уровней ─────────────────────────────────────────────────
# Tested: 2026-06-26 — на дефолте (DEBUG) FIRE скрыт (200 URL → 6 строк вместо ~1000);
# --fire / LOG_LEVEL=FIRE показывает построчный трейс; --quiet прячет FIRE и DEBUG.
def log_fire(msg, emoji=None) -> None:     _emit(FIRE, msg, emoji)
def log_debug(msg, emoji=None) -> None:    _emit(DEBUG, msg, emoji)
def log_info(msg, emoji=None) -> None:     _emit(INFO, msg, emoji)
def log_success(msg, emoji=None) -> None:  _emit(SUCCESS, msg, emoji)
def log_warn(msg, emoji=None) -> None:     _emit(WARN, msg, emoji)
def log_error(msg, emoji=None) -> None:    _emit(ERROR, msg, emoji)


def log_step(msg, emoji="▶") -> None:
    """Старт фазы пайплайна — уровень INFO, выделен цветом (magenta)."""
    if INFO < _LEVEL:
        return
    print(f"{emoji} {_STEP_COLOR}{msg}{_RESET}")


def log_header(title, width: int = 65) -> None:
    """Баннер ═══ для крупного раздела — уровень INFO."""
    if INFO < _LEVEL:
        return
    bar = "═" * width
    print(f"{_HEADER_COLOR}{bar}{_RESET}")
    print(f"{_HEADER_COLOR}  {title}{_RESET}")
    print(f"{_HEADER_COLOR}{bar}{_RESET}")


def log_exc(msg, level: int = ERROR) -> None:
    """Залогировать сообщение + traceback текущего исключения (трейс — на уровне DEBUG)."""
    _emit(level, msg)
    tb = traceback.format_exc()
    if tb and "NoneType: None" not in tb:
        _emit(DEBUG, tb.rstrip())
