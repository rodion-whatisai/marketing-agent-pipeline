"""
TNC Learn — Интерактивное обучение классификатора
==================================================
Читает результат step1.json, показывает откуда пришла каждая классификация,
предлагает новые паттерны для patterns.json, ждёт апрув.

Запуск:
    python learn.py scans/keepbloomingflowers.ca/step1.json
    python learn.py scans/bandago.com/step1.json
"""

import sys
import json
import os
from urllib.parse import urlparse
from page_classifier import save_pattern, load_patterns, ANTHROPIC_API_KEY
from log import log_error, log_success, log_debug, log_step

# ─── Метки источников ────────────────────────────────────────────────────────

SOURCE_LABELS = {
    "regex":        "⚡ regex",
    "patterns_json": "📚 patterns",
    "claude":       "🤖 claude",
    "no_api_key":   "⚠️  no key",
    "fallback":     "❓ fallback",
}

TYPE_LABELS = {
    "lead_form":       "Lead form",
    "booking_confirm": "Booking confirm",
    "quote":           "Quote",
    "checkout":        "Checkout",
    "homepage":        "Homepage",
    "pricing":         "Pricing",
    "location":        "Location",
    "product":         "Product",
    "use_case":        "Use case",
    "search_results":  "Search results",
    "faq_support":     "FAQ / support",
    "careers":         "Careers",
    "about":           "About",
    "blog_content":    "Blog",
    "legal":           "Legal",
    "technical":       "Technical",
    "general":         "General ❓",
}

PRIORITY_LABELS = {
    1: "🔴",
    2: "🟠",
    3: "🟡",
    4: "🟢",
    5: "⚪",
}

# ─── Вывод таблицы классификации ─────────────────────────────────────────────

def print_classification_report(classified: list, domain: str, max_examples: int = 3):
    """
    Группирует по уникальным комбинациям (type, source).
    Показывает max_examples примеров каждой комбинации + счётчик остальных.
    """
    from collections import defaultdict

    log_debug(f"print_classification_report: domain={domain}, items={len(classified)}, max_examples={max_examples}")

    print(f"\n{'═' * 75}")
    print(f"  КЛАССИФИКАЦИЯ — {domain}")
    print(f"{'═' * 75}")

    # Группируем по (type, priority, source)
    groups = defaultdict(list)
    for item in classified:
        ptype = item.get("type", "general")
        pri   = item.get("priority", 5)
        src   = item.get("method", "?")
        key   = (pri, ptype, src)
        groups[key].append(item.get("path", "/"))

    log_debug(f"print_classification_report: {len(groups)} уникальных групп (priority,type,source)")

    # Сортируем: по приоритету, потом по type, потом по source
    print(f"  {'PATH':<42} {'TYPE':<18} {'SRC'}")
    print(f"{'─' * 75}")

    for key in sorted(groups.keys()):
        pri, ptype, src = key
        paths = groups[key]
        pri_label  = PRIORITY_LABELS.get(pri, "⚪")
        type_label = TYPE_LABELS.get(ptype, ptype)
        src_label  = SOURCE_LABELS.get(src, f"  {src[:6]}")

        # Показываем max_examples примеров
        for path in paths[:max_examples]:
            print(f"  {path[:41]:<42} {pri_label} {type_label:<16} {src_label}")

        # Если больше — показываем счётчик
        remainder = len(paths) - max_examples
        if remainder > 0:
            print(f"  {'...' + f' и ещё {remainder}':<42} {pri_label} {type_label:<16} {src_label}")

    print(f"{'─' * 75}")

    # Статистика по источникам
    by_src = {}
    for item in classified:
        src = item.get("method", "?")
        by_src[src] = by_src.get(src, 0) + 1

    print(f"\n  Источники:")
    for src, count in sorted(by_src.items(), key=lambda x: -x[1]):
        label = SOURCE_LABELS.get(src, src)
        print(f"    {label:<20} {count} страниц")


# ─── Интерактивное обучение ───────────────────────────────────────────────────

def suggest_pattern_type(path: str) -> tuple[str, int]:
    """Предлагает тип на основе структуры пути."""
    log_debug(f"suggest_pattern_type: path={path!r}")
    parts = [p for p in path.split("/") if p]

    if not parts:
        log_debug("suggest_pattern_type: пустой путь → homepage")
        return "homepage", 2

    first = parts[0].lower()

    hints = {
        "about": ("about", 3),
        "team": ("about", 3),
        "careers": ("careers", 3),
        "jobs": ("careers", 3),
        "blog": ("blog_content", 4),
        "news": ("blog_content", 4),
        "help": ("faq_support", 3),
        "support": ("faq_support", 3),
        "pricing": ("pricing", 2),
        "plans": ("pricing", 2),
        "contact": ("lead_form", 1),
        "demo": ("lead_form", 1),
    }

    result = hints.get(first, ("general", 5))
    log_debug(f"suggest_pattern_type: first={first!r} → {result}")
    return result


def interactive_learn(classified: list, domain: str):
    """Интерактивный режим — предлагает новые паттерны, ждёт апрув."""

    log_debug(f"interactive_learn: старт, domain={domain}, items={len(classified)}")
    patterns = load_patterns()
    log_debug(f"interactive_learn: загружено {len(patterns)} паттернов из patterns.json")

    # Собираем страницы которые пошли в Claude и вернулись general
    claude_general = [
        item for item in classified
        if item.get("type") == "general" and item.get("method") == "claude"
    ]

    # Собираем страницы которые классифицировал Claude (не general) — новые паттерны
    claude_classified = [
        item for item in classified
        if item.get("method") == "claude" and item.get("type") != "general"
    ]

    log_debug(f"interactive_learn: claude_general={len(claude_general)}, claude_classified={len(claude_classified)}")

    # Собираем паттерны которых нет в patterns.json
    new_patterns = []
    for item in claude_classified:
        path = item.get("path", "")
        parts = [p for p in path.split("/") if p]
        if not parts:
            continue

        # Структурный паттерн — обычно первый сегмент,
        # но для "контейнерных" сегментов берём два (pages/slug, posts/slug и т.д.)
        CONTAINER_SEGMENTS = {
            "pages", "products", "collections", "posts", "p", "en", "fr", "ru",
            "de", "es", "it", "blog", "news", "articles", "items", "listing",
        }
        first = parts[0].lower()
        first_clean = first.rsplit(".", 1)[0] if "." in first else first

        if first_clean in CONTAINER_SEGMENTS and len(parts) >= 2:
            second = parts[1].lower().rsplit(".", 1)[0]
            struct = "/" + first_clean + "/" + second
            log_debug(f"interactive_learn: контейнерный сегмент {first_clean!r} → паттерн {struct}")
        else:
            struct = "/" + first_clean
            log_debug(f"interactive_learn: одиночный сегмент → паттерн {struct}")

        # Пропускаем если паттерн уже в patterns.json
        if struct in patterns:
            log_debug(f"interactive_learn: {struct} уже в patterns.json — пропуск")
            continue

        # Пропускаем если уже добавлен в этот список
        if struct in [p["pattern"] for p in new_patterns]:
            log_debug(f"interactive_learn: {struct} уже в new_patterns — пропуск")
            continue

        # Пропускаем если тип общий и неинформативный
        ptype = item.get("type", "general")
        if ptype in ("legal", "technical") and first_clean in (
            "cookies", "cookie", "privacy", "terms", "tos", "policy",
            "login", "account", "sitemap", "robots"
        ):
            log_debug(f"interactive_learn: {struct} ({ptype}) уже покрыто regex — пропуск")
            continue  # уже покрыто regex — не нужно в patterns.json

        log_debug(f"interactive_learn: новый паттерн-кандидат {struct} → {ptype}")
        new_patterns.append({
            "pattern": struct,
            "example": path,
            "example_url": item.get("url", ""),
            "suggested_type": ptype,
            "suggested_priority": item.get("priority", 5),
            "reason": item.get("reason", ""),
        })
    
    if not new_patterns and not claude_general:
        log_success("Нет новых паттернов для обучения — всё уже известно")
        return

    log_debug(f"interactive_learn: к показу {len(new_patterns)} новых паттернов, {len(claude_general)} general-страниц")
    
    # Показываем новые паттерны от Claude
    # Показываем новые паттерны от Claude
    if new_patterns:
        print(f"\n{'═' * 65}")
        print(f"  🤖 CLAUDE НАШЁЛ {len(new_patterns)} НОВЫХ ПАТТЕРНОВ")
        print(f"  Апрувни — и следующие сайты с такими URL не пойдут в AI")
        print(f"{'═' * 65}\n")
        
        for i, p in enumerate(new_patterns, 1):
            ptype = p["suggested_type"]
            pri   = p["suggested_priority"]
            pri_label = PRIORITY_LABELS.get(pri, "⚪")
            reason = f"\n       Причина: {p['reason']}" if p.get("reason") else ""
            type_display = f"{pri_label} {TYPE_LABELS.get(ptype, ptype)}"

            print(f"  [{i}/{len(new_patterns)}] Паттерн: {p['pattern']}")
            print(f"       Пример URL: {p['example']}")
            print(f"       Claude говорит: {type_display}{reason}")
            print(f"")
            print(f"       y = согласен, добавить в базу")
            print(f"       n = не добавлять")
            print(f"       q = выйти из обучения")
            print(f"       или введи тип вручную: p=product l=lead f=faq a=about pr=pricing s=search")
            
            choice = input(f"\n       Твой выбор [y/n/q/тип]: ").strip().lower()
            log_debug(f"interactive_learn[new_patterns {i}/{len(new_patterns)}]: pattern={p['pattern']}, choice={choice!r}")

            if choice in ("q", "quit", "exit", "stop", "s"):
                log_debug("interactive_learn: пользователь выбрал выход из обучения (new_patterns)")
                print(f"       ⏹  Выход из обучения")
                return

            if choice == "y" or choice == "":
                log_debug(f"interactive_learn: апрув паттерна {p['pattern']} → {ptype} (source=claude_approved)")
                save_pattern(
                    pattern=p["pattern"],
                    page_type=ptype,
                    priority=pri,
                    description=f"Learned from {domain}: {p['example']}",
                    example_url=p["example_url"],
                    source="claude_approved",
                )
                log_success(f"Сохранено: {p['pattern']} → {ptype}", emoji="💾")
            elif choice == "n":
                print(f"       ⏭  Пропущено")
            else:
                # Пользователь ввёл тип вручную
                manual_type = choice
                # Маппинг коротких алиасов
                aliases = {
                    "p": "product", "prod": "product",
                    "l": "lead_form", "lead": "lead_form",
                    "loc": "location",
                    "faq": "faq_support", "f": "faq_support",
                    "pr": "pricing", "price": "pricing",
                    "b": "blog_content", "blog": "blog_content",
                    "a": "about",
                    "t": "technical",
                    "g": "general",
                    "s": "search_results", "search": "search_results",
                }
                final_type = aliases.get(manual_type, manual_type)
                pri_map = {"product": 2, "lead_form": 1, "pricing": 2, "location": 2,
                           "faq_support": 3, "about": 3, "blog_content": 4,
                           "technical": 5, "general": 5, "search_results": 2}
                final_pri = pri_map.get(final_type, 3)
                log_debug(f"interactive_learn: ручной тип {final_type} для {p['pattern']} (priority={final_pri}, source=manual)")
                save_pattern(
                    pattern=p["pattern"],
                    page_type=final_type,
                    priority=final_pri,
                    description=f"Manually typed from {domain}: {p['example']}",
                    example_url=p["example_url"],
                    source="manual",
                )
                log_success(f"Сохранено: {p['pattern']} → {final_type}", emoji="💾")
            print()
    
    # Показываем general страницы — спрашиваем что с ними делать
    if claude_general:
        print(f"\n{'═' * 65}")
        print(f"  ❓ {len(claude_general)} СТРАНИЦ — CLAUDE НЕ ЗНАЕТ ЧТО ЭТО")
        print(f"  Скажи что это — и следующие такие URL пойдут без AI")
        print(f"{'═' * 65}\n")
        
        for item in claude_general:
            path = item.get("path", "")
            suggested_type, suggested_pri = suggest_pattern_type(path)
            log_debug(f"interactive_learn[general]: path={path!r}, guess={suggested_type}")

            print(f"  URL: {path}")
            if suggested_type != "general":
                print(f"  Мой guess: {TYPE_LABELS.get(suggested_type, suggested_type)}")
            print(f"")
            print(f"  Введи тип чтобы обучить классификатор:")
            print(f"  p=product  l=lead_form  f=faq  a=about  pr=pricing")
            print(f"  s=search   loc=location  b=blog  u=use_case  t=technical")
            print(f"  enter = пропустить (оставить general)  q = выйти")
            
            choice = input(f"\n  Твой выбор: ").strip().lower()
            log_debug(f"interactive_learn[general]: choice={choice!r}")

            if choice in ("q", "quit", "exit", "stop"):
                log_debug("interactive_learn: пользователь выбрал выход из обучения (general)")
                print(f"  ⏹  Выход из обучения")
                return

            if choice == "" or choice == "n":
                log_debug("interactive_learn[general]: пропуск (оставляем general)")
                print(f"  ⏭  Пропущено\n")
                continue
            
            if choice == "?":
                print("  Доступные типы: product(p), lead_form(l), pricing(pr),")
                print("  location(loc), faq(f), blog(b), about(a), search(s),")
                print("  use_case(u), technical(t), general(g)")
                choice = input(f"  Тип: ").strip().lower()
            
            aliases = {
                "p": "product", "prod": "product",
                "l": "lead_form", "lead": "lead_form",
                "loc": "location",
                "faq": "faq_support", "f": "faq_support",
                "pr": "pricing", "price": "pricing",
                "b": "blog_content", "blog": "blog_content",
                "a": "about", "u": "use_case",
                "t": "technical", "g": "general",
                "s": "search_results", "search": "search_results",
            }
            final_type = aliases.get(choice, choice)
            pri_map = {"product": 2, "lead_form": 1, "pricing": 2, "location": 2,
                       "faq_support": 3, "about": 3, "blog_content": 4,
                       "technical": 5, "general": 5, "search_results": 2,
                       "use_case": 2}
            final_pri = pri_map.get(final_type, 3)

            # Структурный паттерн — первый сегмент без расширения
            import re as _re
            parts = [p for p in path.split("/") if p]
            first = parts[0].lower() if parts else path.lstrip("/")
            first_clean = _re.sub(r'\.(html|php|htm|asp|aspx)$', '', first)
            struct = "/" + first_clean

            log_debug(f"interactive_learn[general]: ручная классификация {struct} → {final_type} (priority={final_pri}, source=manual)")
            save_pattern(
                pattern=struct,
                page_type=final_type,
                priority=final_pri,
                description=f"Manually classified from {domain}: {path}",
                example_url=item.get("url", ""),
                source="manual",
            )
            log_success(f"Сохранено: {struct} → {TYPE_LABELS.get(final_type, final_type)}", emoji="💾")
            print(f"     Следующие сайты с {struct}/... пойдут без Claude\n")

    log_debug("interactive_learn: обучение завершено, patterns.json обновлён")
    log_success("Обучение завершено. patterns.json обновлён.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def run(step1_file: str):
    log_debug(f"run: открываю step1_file={step1_file}")
    try:
        with open(step1_file, "r", encoding="utf-8") as f:
            step1 = json.load(f)
    except Exception as e:
        log_error(f"Не могу открыть {step1_file}: {e}")
        sys.exit(1)

    base_url = step1.get("base_url", "")
    domain = urlparse(base_url).netloc
    classified = step1.get("classified", [])
    log_debug(f"run: base_url={base_url!r}, domain={domain!r}, classified={len(classified)}")

    if not classified:
        log_error(f"Нет данных классификации в {step1_file}")
        sys.exit(1)

    # Шаг 1: показываем таблицу с источниками
    print_classification_report(classified, domain)

    # Шаг 2: интерактивное обучение
    log_step("Запускаю режим обучения...", emoji="🎓")
    interactive_learn(classified, domain)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python learn.py scans/[domain]/step1.json")
        sys.exit(1)
    
    run(sys.argv[1])
