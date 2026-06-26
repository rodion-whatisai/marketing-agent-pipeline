"""
Демо логирования — показывает все уровни и цвета.
Запуск:
    python log_demo.py            # дефолт INFO — debug скрыт
    python log_demo.py --debug    # видно ВСЁ, включая debug + трейс
"""

import sys
from utils import setup_console
import log

setup_console()  # UTF-8 + включить цвета на Windows

if "--debug" in sys.argv:
    log.set_level("DEBUG")

log.log_header("ДЕМО ЛОГИРОВАНИЯ")
log.log_step("Это log_step — старт фазы пайплайна", emoji="🌐")
log.log_info("Это log_info — обычная информация")
log.log_success("Это log_success — успех/готово")
log.log_warn("Это log_warn — предупреждение")
log.log_error("Это log_error — ошибка")
log.log_debug("Это log_debug — виден ТОЛЬКО при --debug (или LOG_LEVEL=DEBUG)")

try:
    1 / 0
except Exception:
    log.log_exc("log_exc — ошибка с трейсом (трейс на уровне DEBUG)")

print()
print("Текущий уровень:", "DEBUG (видно всё)" if "--debug" in sys.argv else "INFO (debug скрыт — запусти с --debug чтобы увидеть его)")
