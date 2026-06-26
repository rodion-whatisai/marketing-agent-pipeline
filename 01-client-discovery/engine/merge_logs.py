"""
TNC Pipeline — Merge Logs
=========================
Склеивает логи всех шагов в один файл.

Запуск:
    python merge_logs.py redacted-client.example
"""

import sys
from utils import merge_logs, setup_console
from log import log_info, log_success, log_debug

if __name__ == "__main__":
    setup_console()  # UTF-8 + ANSI на Windows (фикс cp1252-крэша при standalone-запуске)
    log_debug(f"merge_logs.py entry: argv={sys.argv}")
    if len(sys.argv) < 2:
        log_info("Использование: python merge_logs.py <domain>")
        sys.exit(1)

    domain = sys.argv[1].strip().rstrip("/")
    log_debug(f"merge_logs.py: domain={domain}")
    merged = merge_logs(domain)
    log_debug(f"merge_logs.py: merge_logs() вернул {merged}")
    log_success(f"Общий лог сохранён: {merged}")
