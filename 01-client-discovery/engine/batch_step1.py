"""
TNC Pipeline — Batch Step 1
============================
Запускает step1 для списка доменов без интерактива.
Результат: таблица в терминале + CSV файл.

Запуск:
    python batch_step1.py aerosus.fr arnotteurope.com airsus.fr ...
    python batch_step1.py --file domains.txt
    python batch_step1.py aerosus.fr arnotteurope.com --no-discovery
"""

import sys
import json
import csv
import argparse
import traceback
from pathlib import Path
from datetime import datetime
from io import StringIO
from unittest.mock import patch

# ─── UTF-8 stdout/stderr (Windows cp1252 fix) ────────────────────────────────
# Переводит вывод в UTF-8, чтобы не требовать флаг `python -X utf8`.
# Нужно для эмодзи и кириллицы в логах step1.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
# Включаем ANSI/VT на Windows — чтобы цвета логов рендерились в батч-прогонах.
try:
    import colorama
    colorama.just_fix_windows_console()
except Exception:
    pass

# ─── Патчим интерактивные части step1 до импорта ─────────────────────────────

# learn.py — пропускаем
import unittest.mock as mock

_learn_patch = mock.MagicMock()
sys.modules.setdefault("learn", _learn_patch)

# Патчим input() глобально — отвечаем "1" на все вопросы (сканировать всё)
_original_input = input

def _auto_input(prompt=""):
    print(f"[batch] auto-answer '1' to: {prompt.strip()}")
    return "1"


# ─── Импорт шага 1 ───────────────────────────────────────────────────────────

try:
    import step1_sitemap as step1
except ImportError as e:
    print(f"❌ Не могу импортировать step1_sitemap.py: {e}")
    print("   Убедись что batch_step1.py лежит рядом с step1_sitemap.py")
    sys.exit(1)

from utils import normalize_url, scan_path


def playwright_homepage_crawl(base_url: str, site_domain: str) -> list:
    """
    Лёгкий Playwright crawl главной страницы.
    Запускается когда sitemap дал 0 URL — обходит JS-рендеринг и блокировки.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  ⚠️  Playwright не установлен")
        return []

    print(f"  🌐 Playwright fallback: открываю {base_url}...")
    urls = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1440, "height": 900},
            )
            page = context.new_page()
            try:
                page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
            except Exception as e:
                print(f"  ⚠️  Playwright error: {e}")
                browser.close()
                return []

            all_links = set()
            for scroll_pos in [0, 0.5, 1.0]:
                try:
                    page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {scroll_pos})")
                    page.wait_for_timeout(500)
                    links = page.evaluate("""
                        () => Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => h.startsWith('http'))
                            .slice(0, 500)
                    """)
                    all_links.update(links)
                except Exception:
                    pass

            browser.close()

            skip_ext = {".jpg", ".jpeg", ".png", ".gif", ".svg", ".pdf",
                        ".zip", ".css", ".js", ".ico", ".webp", ".mp4"}
            seen = set()
            for u in all_links:
                u = u.split("?")[0].split("#")[0].rstrip("/")
                if not u or site_domain not in u:
                    continue
                if any(u.lower().endswith(ext) for ext in skip_ext):
                    continue
                if u.lower() not in seen:
                    seen.add(u.lower())
                    urls.append(u)

            print(f"  ✓ Playwright нашёл {len(urls)} URL")
    except Exception as e:
        print(f"  ⚠️  Playwright fallback error: {e}")
    return urls


# ─── Хелперы ─────────────────────────────────────────────────────────────────

PRIORITY_LABELS = {
    1: "CRITICAL",
    2: "HIGH",
    3: "MEDIUM",
    4: "LOW",
    5: "SKIP",
}

def summarize_result(domain: str, result: dict) -> dict:
    """Вытаскивает ключевые поля из результата step1."""
    platform_info = result.get("platform", {})
    platform = platform_info.get("platform", "unknown") if isinstance(platform_info, dict) else str(platform_info)

    lang_info = result.get("site_language", {})
    if isinstance(lang_info, dict):
        lang = lang_info.get("lang", "?")
        bilingual = lang_info.get("bilingual", False)
    else:
        lang = str(lang_info)
        bilingual = False

    classified = result.get("classified", [])
    to_scan = result.get("to_scan", [])

    # Подсчёт по приоритетам
    by_priority = {}
    for page in classified:
        p = page.get("priority", 5)
        by_priority[p] = by_priority.get(p, 0) + 1

    critical = by_priority.get(1, 0)
    high = by_priority.get(2, 0)
    medium = by_priority.get(3, 0)
    low = by_priority.get(4, 0)
    skip = by_priority.get(5, 0)

    # Типы страниц в to_scan
    page_types = sorted(set(p.get("type", "?") for p in to_scan))

    # Соцсети (кроме facebook_accounts — это отдельный dict, не URL)
    social = result.get("social", {})
    socials_found = [k for k, v in social.items() if v and k != "facebook_accounts"]

    # ── Facebook Ads Library ────────────────────────────────────
    fb = social.get("facebook_accounts", {})
    if not isinstance(fb, dict):
        fb = {}
    site_country = fb.get("site_country", "?") or "?"
    accounts = fb.get("accounts", []) or []
    alive = [a for a in accounts if isinstance(a, dict) and a.get("alive")]

    # Топ-аккаунт — с максимальным active_ads_count
    top = max(alive, key=lambda a: a.get("active_ads_count") or 0, default=None)

    if top:
        fb_handle = top.get("handle", "") or ""
        fb_display_name = top.get("display_name", "") or ""
        fb_url = top.get("url", "") or ""
        active_ads = top.get("active_ads_count") or 0
        partnership = top.get("partnership_count") or 0
        n_ad_texts = len(top.get("ad_texts", []) or [])
        n_images = len(top.get("saved_images", []) or [])
        ads_library_url = (top.get("ads_library", {}) or {}).get("ALL", {}).get("active_only", "") or ""
    else:
        fb_handle = ""
        fb_display_name = ""
        fb_url = ""
        active_ads = 0
        partnership = 0
        n_ad_texts = 0
        n_images = 0
        ads_library_url = ""

    # Остальные живые аккаунты — "handle:ads_count"
    others = [
        f"{a.get('handle','?')}:{a.get('active_ads_count') or 0}"
        for a in alive if a is not top
    ]
    other_fb_accounts = "; ".join(others)

    fb_json_path = str(scan_path(domain, "fb.json"))

    # Sitemap source
    source = result.get("sitemap_source", "?")
    if len(source) > 40:
        source = source[:37] + "..."

    return {
        "domain": domain,
        "status": "✅ OK",
        "country": site_country,
        "platform": platform,
        "language": lang + (" (bilingual)" if bilingual else ""),
        "active_ads": active_ads,
        "partnership": partnership,
        "fb_handle": fb_handle,
        "fb_display_name": fb_display_name,
        "fb_url": fb_url,
        "ads_library_url": ads_library_url,
        "n_ad_texts": n_ad_texts,
        "n_images": n_images,
        "other_fb_accounts": other_fb_accounts,
        "total_urls": result.get("total_found", 0),
        "to_scan": len(to_scan),
        "critical": critical,
        "high": high,
        "medium": medium,
        "low": low,
        "skip": skip,
        "page_types": ", ".join(page_types[:8]) + ("..." if len(page_types) > 8 else ""),
        "socials": ", ".join(socials_found) if socials_found else "—",
        "sitemap_source": source,
        "step1_json": str(scan_path(domain, f"{domain}_step1.json")),
        "fb_json_path": fb_json_path,
    }


def load_existing_step1(domain: str) -> dict | None:
    """Загружает уже существующий step1 JSON если есть."""
    path = scan_path(domain, f"{domain}_step1.json")
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def print_table(rows: list[dict]):
    """Красивая таблица в терминал."""
    if not rows:
        return

    cols = [
        ("domain",          "Domain",          28),
        ("status",          "Status",           8),
        ("country",         "CC",               4),
        ("platform",        "Platform",         10),
        ("language",        "Lang",              8),
        ("active_ads",      "Ads",               5),
        ("partnership",     "Part",              5),
        ("fb_handle",       "FB Handle",        18),
        ("n_ad_texts",      "Txt",               4),
        ("n_images",        "Img",               4),
    ]

    header = "  ".join(label.ljust(width) for _, label, width in cols)
    sep = "  ".join("─" * width for _, _, width in cols)

    print(f"\n{'═' * len(sep)}")
    print("  BATCH STEP 1 — RESULTS")
    print(f"{'═' * len(sep)}")
    print(header)
    print(sep)

    for row in rows:
        line = "  ".join(
            str(row.get(key, "—"))[:width].ljust(width)
            for key, _, width in cols
        )
        print(line)

    print(sep)
    ok = sum(1 for r in rows if "OK" in r.get("status", ""))
    err = len(rows) - ok
    print(f"\n  ✅ OK: {ok}   ❌ Error: {err}   Total: {len(rows)}")


def save_csv(rows: list[dict], output_path: Path):
    """Сохраняет все поля в CSV. Берём union ключей чтобы не терять поля из error rows."""
    if not rows:
        return
    # Порядок: первая строка задаёт основные колонки, потом всё остальное
    fields = list(rows[0].keys())
    seen = set(fields)
    for r in rows[1:]:
        for k in r.keys():
            if k not in seen:
                fields.append(k)
                seen.add(k)
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"\n💾 CSV сохранён: {output_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def _make_error_row(domain: str, status: str, error: str = "") -> dict:
    return {
        "domain": domain,
        "status": status,
        "country": "?",
        "platform": "?",
        "language": "?",
        "active_ads": 0,
        "partnership": 0,
        "fb_handle": "",
        "fb_display_name": "",
        "fb_url": "",
        "ads_library_url": "",
        "n_ad_texts": 0,
        "n_images": 0,
        "other_fb_accounts": "",
        "total_urls": 0,
        "to_scan": 0,
        "critical": 0,
        "high": 0,
        "medium": 0,
        "low": 0,
        "skip": 0,
        "page_types": "",
        "socials": "?",
        "sitemap_source": "?",
        "step1_json": "",
        "fb_json_path": "",
        "error": error,
    }


def run_batch(domains: list[str], skip_discovery: bool = True, force_rerun: bool = False):
    """
    skip_discovery=True по умолчанию — в батче Playwright discovery слишком медленный
    (rate limit 15с × N страниц). Используй --discovery чтобы включить.
    """
    results = []
    errors = []
    _stop_all = False  # второй Ctrl+C — стоп всего батча

    print(f"\n🚀 Batch Step 1 — {len(domains)} доменов")
    print(f"   skip_discovery={skip_discovery}  force_rerun={force_rerun}")
    print(f"   💡 Ctrl+C — пропустить домен  |  Ctrl+C дважды — остановить батч\n")

    for i, domain in enumerate(domains, 1):
        if _stop_all:
            break

        domain = domain.strip().lower()
        if not domain:
            continue

        print(f"\n{'─' * 60}")
        print(f"[{i}/{len(domains)}] {domain}")
        print(f"{'─' * 60}")

        # Если уже есть результат — можно пропустить
        if not force_rerun:
            existing = load_existing_step1(domain)
            if existing:
                print(f"  ⏩ Найден существующий step1.json — пропускаю (--rerun чтобы перезапустить)")
                row = summarize_result(domain, existing)
                row["status"] = "⏩ cached"
                results.append(row)
                continue

        try:
            # Патчим input() только на время вызова run()
            with patch("builtins.input", _auto_input):
                result = step1.run(
                    domain,
                    limit=9999,          # не ограничиваем
                    force_all=True,      # не спрашиваем
                    show_all_in_list=False,
                    skip_discovery=skip_discovery,
                )

            # ── Playwright fallback если 0 URL ──────────────────
            if result.get("total_found", 0) == 0:
                print(f"\n  ⚠️  0 URL из sitemap — пробую Playwright...")
                from urllib.parse import urlparse as _urlparse
                import json as _json
                _base_url = result.get("base_url", f"https://{domain}")
                _site_domain = _urlparse(_base_url).netloc
                _pw_urls = playwright_homepage_crawl(_base_url, _site_domain)
                if _pw_urls:
                    from page_classifier import classify_urls
                    _classified = classify_urls(_pw_urls)
                    _to_scan = [x for x in _classified if x.get("priority", 5) <= 2]
                    result["total_found"] = len(_pw_urls)
                    result["classified"] = _classified
                    result["to_scan"] = _to_scan
                    result["sitemap_source"] = "playwright homepage crawl (fallback)"
                    _json_path = scan_path(domain, f"{domain}_step1.json")
                    with open(_json_path, "w", encoding="utf-8") as _f:
                        _json.dump(result, _f, indent=2, ensure_ascii=False)
                    print(f"  ✓ {len(_pw_urls)} URL найдено, {len(_to_scan)} к сканированию")
                else:
                    print(f"  ❌ Playwright тоже дал 0 — сайт недоступен или пустой")

            row = summarize_result(domain, result)
            results.append(row)
            print(f"\n  ✅ Done: {len(result.get('to_scan', []))} страниц к сканированию")

        except KeyboardInterrupt:
            # Первый Ctrl+C — пропускаем домен
            print(f"\n⚠️  Ctrl+C — пропускаю {domain}, перехожу к следующему")
            print(f"   (Ctrl+C ещё раз в течение 2с — остановить весь батч)")
            errors.append(_make_error_row(domain, "⏭ skipped", "KeyboardInterrupt"))

            # Даём 2 секунды на второй Ctrl+C
            import time
            try:
                time.sleep(2)
            except KeyboardInterrupt:
                _stop_all = True
                print(f"\n🛑 Батч остановлен пользователем")

        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n  ❌ Ошибка: {e}")
            print(f"     {tb.splitlines()[-1]}")
            errors.append(_make_error_row(domain, "❌ error", str(e)))

    all_rows = results + errors

    # Терминальная таблица
    print_table(all_rows)

    # CSV
    ts = datetime.now().strftime("%Y%m%d_%H%M")
    csv_path = Path("scans") / f"batch_step1_{ts}.csv"
    csv_path.parent.mkdir(exist_ok=True)
    save_csv(all_rows, csv_path)

    # Детальный JSON на случай дальнейшей обработки
    json_path = Path("scans") / f"batch_step1_{ts}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(all_rows, f, indent=2, ensure_ascii=False)
    print(f"💾 JSON сохранён: {json_path}")

    return all_rows


# ─── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="TNC Batch Step 1 — запускает step1 для списка доменов",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="""
Примеры:
  python batch_step1.py aerosus.fr arnotteurope.com airsus.fr
  python batch_step1.py --file domains.txt
  python batch_step1.py aerosus.fr --rerun
  python batch_step1.py aerosus.fr --discovery   # Playwright discovery (медленно)

Управление во время запуска:
  Ctrl+C         — пропустить текущий домен, продолжить батч
  Ctrl+C дважды  — остановить весь батч
        """
    )
    parser.add_argument(
        "domains", nargs="*",
        help="Домены через пробел"
    )
    parser.add_argument(
        "--file", "-f",
        help="Файл с доменами (один домен на строку)"
    )
    parser.add_argument(
        "--discovery", action="store_true",
        help="Включить Playwright pattern discovery. По умолчанию ВЫКЛЮЧЕН в батче (rate limits)"
    )
    parser.add_argument(
        "--rerun", action="store_true",
        help="Перезапустить даже если step1.json уже есть"
    )
    parser.add_argument("--debug", action="store_true", help="Полный отладочный лог (как LOG_LEVEL=DEBUG)")
    args = parser.parse_args()

    if args.debug:
        import log
        log.set_level("DEBUG")

    domains = list(args.domains)

    if args.file:
        try:
            with open(args.file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        domains.append(line)
        except FileNotFoundError:
            print(f"❌ Файл не найден: {args.file}")
            sys.exit(1)

    if not domains:
        parser.print_help()
        sys.exit(1)

    # Дедупликация
    seen = set()
    unique_domains = []
    for d in domains:
        d = d.strip().lower()
        if d and d not in seen:
            seen.add(d)
            unique_domains.append(d)

    run_batch(
        unique_domains,
        skip_discovery=not args.discovery,  # default: discovery ВЫКЛЮЧЕН
        force_rerun=args.rerun,
    )
