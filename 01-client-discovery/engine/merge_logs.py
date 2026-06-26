"""
TNC Pipeline — Merge Logs
=========================
Склеивает логи всех шагов в один файл.

Запуск:
    python merge_logs.py redacted-client.example
"""

import sys
from utils import merge_logs, setup_console

if __name__ == "__main__":
    setup_console()  # UTF-8 + ANSI на Windows (фикс cp1252-крэша при standalone-запуске)
    if len(sys.argv) < 2:
        print("Использование: python merge_logs.py <domain>")
        sys.exit(1)

    domain = sys.argv[1].strip().rstrip("/")
    merged = merge_logs(domain)
    print(f"✅ Общий лог сохранён: {merged}")
