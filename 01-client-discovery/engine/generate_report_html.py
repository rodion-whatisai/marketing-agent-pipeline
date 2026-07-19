"""
TNC Report Generator — HTML v3 (эталонный, truth-first)
========================================================
Читает step2.json и генерирует client-facing HTML отчёт.

Ядро отчёта — модуль report_truth (две таблицы: что установлено + воронка
событий, с подсказками «как для бабушки»). Плюс перенесённые факты:
внешние сервисы и постраничный срез. Никаких вердиктов/оценок/рекомендаций —
только сухая правда о том, что мы наблюдали (решение Rodion'а 2026-07-14).

Старый отчёт (вердикт, блоки Google Tools/Платформы, GAP-карточки, OK-секция,
рекомендации) выброшен: новые таблицы покрывают это честнее.
"""

import sys
import json
import os
import html as _html
import webbrowser
from datetime import datetime
from urllib.parse import urlparse

from log import log_info, log_error, log_debug, log_success, log_step

import report_truth


def esc(s):
    return _html.escape(str(s))


def load_json(path: str) -> dict:
    log_debug(f"load_json: чтение {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        log_debug(f"load_json: загружено {len(data)} top-level ключей из {path}")
        return data
    except Exception as e:
        log_error(f"Не могу открыть {path}: {e}")
        sys.exit(1)


def load_gtm(domain: str, step2_dir: str) -> dict:
    log_debug(f"load_gtm: domain={domain}, step2_dir={step2_dir}")
    gtm_path = os.path.join(step2_dir, "gtm.json")
    if os.path.exists(gtm_path):
        log_debug(f"load_gtm: найден {gtm_path}, парсинг")
        try:
            with open(gtm_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log_debug(f"load_gtm: не удалось распарсить {gtm_path}: {e}")
            pass
    else:
        log_debug(f"load_gtm: {gtm_path} отсутствует")
    return {}


def load_step1_stats(domain: str, step2_dir: str) -> dict:
    """Читает step1.json — кол-во страниц и категории."""
    log_debug(f"load_step1_stats: domain={domain}, step2_dir={step2_dir}")
    step1_path = os.path.join(step2_dir, f"{domain}_step1.json")
    result = {"total": 0, "categories": {}}
    if os.path.exists(step1_path):
        log_debug(f"load_step1_stats: найден {step1_path}, парсинг")
        try:
            with open(step1_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            classified = d.get("classified", [])
            result["total"] = len(classified)
            log_debug(f"load_step1_stats: classified pages = {result['total']}")
            from collections import Counter
            type_counts = Counter(p.get("type", "general") for p in classified)
            TYPE_NAMES = {
                "lead_form": "формы/заявки",
                "homepage": "главная",
                "product": "продукты/коллекции",
                "about": "о компании",
                "location": "локации",
                "blog_content": "блог",
                "faq_support": "FAQ",
                "legal": "политики",
                "pricing": "цены",
                "checkout": "оформление",
                "general": "прочие",
            }
            result["categories"] = {
                TYPE_NAMES.get(t, t): c
                for t, c in type_counts.most_common()
                if t not in ("legal", "general")
            }
            log_debug(f"load_step1_stats: категорий собрано = {len(result['categories'])}")
        except Exception as e:
            log_debug(f"load_step1_stats: не удалось распарсить {step1_path}: {e}")
            pass
    else:
        log_debug(f"load_step1_stats: {step1_path} отсутствует")
    return result


# ─── Внешние сервисы (перенесённый факт) ─────────────────────────────────────
ANALYTICS_TOOLS = {"Microsoft Clarity", "Hotjar", "FullStory"}


def _external_block(external_services: dict) -> str:
    if not external_services:
        return ""
    rows = ""
    for svc, where in external_services.items():
        pages = where if isinstance(where, (list, tuple, set)) else []
        where_str = ", ".join(esc(x) for x in list(pages)[:3]) or "—"
        if svc in ANALYTICS_TOOLS:
            note = "Запись сессий / тепловые карты."
        else:
            note = "Конверсия происходит на стороне сервиса — пиксели её не видят."
        rows += (f'<tr><td class="rt-name">{esc(svc)}</td>'
                 f'<td class="rt-ok">{note}</td>'
                 f'<td class="rt-mut">{where_str}</td></tr>')
    return (f'<section class="rt"><h2 class="rt-h2">Замечено на сайте — внешние сервисы</h2>'
            f'<table class="rt-tbl"><thead><tr><th>Сервис</th><th>Что это значит</th>'
            f'<th>Где</th></tr></thead><tbody>{rows}</tbody></table></section>')


def generate_html(data: dict, gtm_data: dict = None) -> str:
    log_debug("generate_html: старт рендера отчёта (truth-first v3)")
    gtm_data = gtm_data or {}
    base_url = data.get("base_url", "")
    domain = urlparse(base_url).netloc or base_url
    date_str = datetime.now().strftime("%d.%m.%Y")
    all_pages = data.get("all_pages", [])
    scanned = data.get("scanned", len(all_pages))
    sitemap_total = data.get("sitemap_total", 0)
    cats = data.get("sitemap_categories", {}) or {}
    external_services = data.get("external_services", {}) or {}

    # объём скана (перенесённый факт): отобрано для аудита из полного sitemap → сколько прошли
    selected = data.get("sitemap_deduped") or data.get("sitemap_poi") or scanned
    scope = f"Отобрано для аудита: {selected}"
    if sitemap_total:
        scope += f" из {sitemap_total} страниц сайта"
    scope += " (приоритетные страницы каждой категории)"
    if scanned != selected:
        scope += f". Просканировано: {scanned}"
    cats_str = ", ".join(f"{esc(n)} ({c})" for n, c in list(cats.items())[:5])
    if cats_str:
        scope += f". Категории: {cats_str}"

    truth_block = report_truth.render(all_pages, gtm_data, domain)   # ядро (+ CSS)
    external_block = _external_block(external_services)
    pages_block = report_truth.render_pages(all_pages)

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Аудит трекинга — {esc(domain)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family:"IBM Plex Sans",-apple-system,Segoe UI,Roboto,sans-serif;
    background:#0e0f13; color:#d7dae0; line-height:1.6; min-height:100vh; }}
  .doc {{ max-width:940px; margin:0 auto; padding:28px 40px 60px; }}
  .head {{ border-bottom:1px solid #23262e; padding-bottom:16px; margin-bottom:8px; }}
  .head .label {{ font-family:"IBM Plex Mono",monospace; font-size:11px; color:#5b7fff;
    letter-spacing:.15em; text-transform:uppercase; }}
  .head h1 {{ font-size:22px; font-weight:600; color:#fff; margin-top:4px; }}
  .head .date {{ font-family:"IBM Plex Mono",monospace; font-size:12px; color:#7a7a8a; margin-top:2px; }}
  .scope {{ color:#9aa0ab; font-size:13px; margin:14px 0 4px; }}
</style>
</head>
<body>
  <div class="doc">
    <div class="head">
      <div class="label">TNC · Аудит трекинга</div>
      <h1>{esc(domain)}</h1>
      <div class="date">{date_str}</div>
    </div>
    <p class="scope">{scope}</p>
    {truth_block}
    {external_block}
    {pages_block}
  </div>
</body>
</html>"""


def run(step2_path: str):
    log_step(f"Генерация HTML отчёта из {step2_path}", emoji="🌐")
    data = load_json(step2_path)
    step2_dir = os.path.dirname(step2_path)
    domain = urlparse(data.get("base_url", "")).netloc
    log_debug(f"run: domain={domain}, step2_dir={step2_dir}")

    gtm_data = load_gtm(domain, step2_dir)
    step1_stats = load_step1_stats(domain, step2_dir)
    data["sitemap_total"] = step1_stats["total"]
    data["sitemap_categories"] = step1_stats["categories"]
    html = generate_html(data, gtm_data)

    out_path = os.path.join(step2_dir, f"{domain}_report.html")
    log_debug(f"run: запись отчёта в {out_path}")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    log_success(f"HTML отчёт: {out_path}")
    log_debug(f"run: открываю отчёт в браузере: {out_path}")
    try:
        webbrowser.open(f"file://{os.path.abspath(out_path)}")
    except Exception as e:
        log_debug(f"run: не удалось открыть браузер: {e}")
        pass

    return out_path


if __name__ == "__main__":
    from utils import setup_console
    setup_console()  # UTF-8 + ANSI на Windows (фикс cp1252-крэша при standalone-запуске)
    if len(sys.argv) < 2:
        log_info("Usage: python generate_report_html.py scans/domain/step2.json")
        sys.exit(1)
    run(sys.argv[1])
