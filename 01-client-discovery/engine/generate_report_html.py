"""
TNC Report Generator — HTML v2
================================
Читает step2.json и генерирует HTML отчёт.
Структура: заголовок → инструменты → платформы → события → GAP страницы → внешние сервисы
"""

import sys
import json
import os
import webbrowser
from datetime import datetime
from urllib.parse import urlparse
from pathlib import Path
from collections import defaultdict


def load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"❌ Не могу открыть {path}: {e}")
        sys.exit(1)


def load_gtm(domain: str, step2_dir: str) -> dict:
    gtm_path = os.path.join(step2_dir, "gtm.json")
    if os.path.exists(gtm_path):
        try:
            with open(gtm_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_step1_stats(domain: str, step2_dir: str) -> dict:
    """Читает step1.json — кол-во страниц и категории."""
    step1_path = os.path.join(step2_dir, f"{domain}_step1.json")
    result = {"total": 0, "categories": {}}
    if os.path.exists(step1_path):
        try:
            with open(step1_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            classified = d.get("classified", [])
            result["total"] = len(classified)
            from collections import Counter
            type_counts = Counter(p.get("type", "general") for p in classified)
            # Человекочитаемые названия категорий
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
        except Exception:
            pass
    return result


def page_label(path: str, ptype: str) -> str:
    if path == "/" or ptype == "homepage":
        return "Главная страница"
    labels = {
        "lead_form": "Форма / заявка",
        "product": "Страница продукта",
        "checkout": "Оформление заказа",
        "booking_confirm": "Подтверждение",
        "location": "Локация / студия",
        "pricing": "Цены / пакеты",
        "about": "О компании",
        "faq_support": "FAQ",
        "blog_content": "Блог",
    }
    return labels.get(ptype, path)


NOISE_EVENTS = {
    "Meta": ["PageView", "fired"],
    "Google Analytics": ["gtm.init", "gtm.js", "page_view", "user_engagement",
                         "session_start", "first_visit", "scroll", "fired"],
    "Google Ads": [],
    "TikTok": ["fired"],
    "Bing/Microsoft": ["fired"],
}

def is_noise(plat, ev):
    return ev in NOISE_EVENTS.get(plat, [])


def generate_html(data: dict, gtm_data: dict = None) -> str:
    base_url = data.get("base_url", "")
    domain = urlparse(base_url).netloc or base_url
    date_str = datetime.now().strftime("%d.%m.%Y")

    scanned        = data.get("scanned", 0)
    sitemap_total  = data.get("sitemap_total", 0)
    sitemap_cats   = data.get("sitemap_categories", {})
    # Строка с категориями — топ 4
    cats_str = ", ".join(f"{name} ({cnt})" for name, cnt in list(sitemap_cats.items())[:4])
    sitemap_poi    = data.get("sitemap_poi", scanned)
    sitemap_deduped= data.get("sitemap_deduped", scanned)
    lang_removed   = data.get("lang_removed", 0)
    lang_prefixes  = data.get("lang_prefixes", [])
    lang_sub = f"только EN ({lang_removed} {', '.join(lang_prefixes).upper()} убрано как дубли)" if lang_removed > 0 else "приоритетные страницы каждой категории"
    oks            = data.get("oks", 0)
    gaps        = data.get("gaps", 0)
    no_tracking = data.get("no_tracking", 0)
    gtm_platforms_raw = data.get("gtm_platforms", [])
    external_services = data.get("external_services", {})
    gap_pages   = data.get("gap_pages", [])
    ok_pages    = data.get("ok_pages", [])
    all_pages   = data.get("all_pages", [])
    unverified_pages = data.get("unverified_pages", [])
    dup_pages   = [p for p in all_pages if p.get("duplicate_pixels")]

    # GTM info
    gtm_ids = list(gtm_data.keys()) if gtm_data else []
    gtm_id_str = gtm_ids[0] if gtm_ids else None

    # GTM платформы из контейнера
    gtm_container_platforms = set()
    for container in (gtm_data or {}).values():
        for p in container.get("platforms_found", {}):
            gtm_container_platforms.add(p)

    # Нормализация названий
    PLAT_NORM = {
        "Google Analytics GA4": "Google Analytics",
        "Meta Pixel": "Meta",
        "Microsoft/Bing": "Bing/Microsoft",
    }
    gtm_platforms = {PLAT_NORM.get(p, p) for p in gtm_platforms_raw}
    gtm_container_norm = {PLAT_NORM.get(p, p) for p in gtm_container_platforms}

    # Платформы найденные в network
    network_platforms = set()
    shopify_platforms = set()
    presence_platforms = set()   # пиксель пойман по ID (присутствие) — ловится даже в headless
    for page in all_pages:
        for plat in page.get("pixel_events", {}):
            network_platforms.add(plat)
        for plat in page.get("shopify_pixel_platforms", []):
            shopify_platforms.add(plat)
        for plat in page.get("pixel_ids", {}):
            presence_platforms.add(plat)

    all_found = network_platforms | shopify_platforms

    # Три ключевые метрики
    pages_with_cta = sum(1 for p in all_pages if p.get("cta_elements"))
    # "Имеют пиксель" = пиксель установлен (присутствие), не "событие сработало".
    # pixel_ids ловит Meta по ID даже в headless, где beacon события подавлён.
    # Tested: 2026-06-26 on nissan.ie — robot ловил оба Meta-ID, счётчик флипает 0→4 of 4.
    pages_with_pixel = sum(
        1 for p in all_pages
        if p.get("pixel_events") or p.get("shopify_pixel_platforms") or p.get("pixel_ids")
    )
    pages_with_conversion = sum(
        1 for p in all_pages
        if any(
            ev.get("is_conversion") or (
                ev.get("event", "") not in ("PageView", "fired", "track", "unknown")
                and not ev.get("is_noise")
            )
            for evs in p.get("pixel_events", {}).values()
            for ev in evs
        )
    )

    # Вердикт
    if no_tracking > 0 and not all_found:
        verdict_cls = "critical"
        verdict_text = "Трекинг отсутствует"
    elif gaps > 0:
        verdict_cls = "warning"
        verdict_text = "Есть пробелы в трекинге"
    elif oks > 0:
        verdict_cls = "ok"
        verdict_text = "Трекинг настроен"
    else:
        verdict_cls = "warning"
        verdict_text = "Требует проверки"

    # ── Google Tools блок ──────────────────────────────────────────
    gtm_row = ""
    if gtm_id_str:
        gtm_row = f'<div class="tool-row"><span class="tool-label">Google Tag Manager</span><span class="tool-val active">{gtm_id_str}</span></div>'
    else:
        gtm_row = f'<div class="tool-row"><span class="tool-label">Google Tag Manager</span><span class="tool-val missing">Не найден</span></div>'

    ga4_id = next((i for i in gtm_platforms if i.startswith("G-")), None)
    has_ga4 = "Google Analytics" in gtm_platforms or "Google Analytics" in all_found
    ga4_row = f'<div class="tool-row"><span class="tool-label">Google Analytics (GA4)</span><span class="tool-val {"active" if has_ga4 else "missing"}">{"Установлен" if has_ga4 else "Не найден"}</span></div>'

    has_ads = "Google Ads" in gtm_platforms or "Google Ads" in all_found
    ads_row = f'<div class="tool-row"><span class="tool-label">Google Ads</span><span class="tool-val {"active" if has_ads else "missing"}">{"Установлен" if has_ads else "Не найден"}</span></div>'

    # ── Платформы блок ─────────────────────────────────────────────
    PLATFORMS_ORDER = ["Meta", "TikTok", "Bing/Microsoft", "LinkedIn", "Snapchat", "Pinterest"]
    plat_rows = ""
    for plat in PLATFORMS_ORDER:
        in_network   = plat in network_platforms
        in_shopify   = plat in shopify_platforms
        in_presence  = plat in presence_platforms
        in_gtm_cont  = plat in gtm_container_norm

        if in_network:
            cls, status = "active", "Установлен — виден в сети"
        elif in_shopify:
            cls, status = "active", "Установлен (Shopify web-pixels)"
        elif in_presence:
            cls, status = "active", "Установлен — виден по ID (событие не зафиксировано)"
        elif in_gtm_cont:
            cls, status = "warning", "Есть в GTM — при загрузке не зафиксирован"
        else:
            cls, status = "missing", "Не найден"

        plat_rows += f'<div class="plat-row"><span class="plat-name">{plat}</span><span class="plat-status {cls}">{status}</span></div>'

    # ── GAP страницы ───────────────────────────────────────────────
    TYPE_ORDER = ["lead_form", "booking_confirm", "quote", "checkout", "homepage",
                  "product", "location", "pricing", "use_case", "search_results",
                  "about", "general"]

    gap_by_type = defaultdict(list)
    for p in gap_pages:
        gap_by_type[p.get("page_type", "general")].append(p)

    gap_sections_html = ""
    for ptype in TYPE_ORDER:
        pages = gap_by_type.get(ptype, [])
        if not pages:
            continue

        type_labels = {
            "lead_form": "Формы и заявки",
            "booking_confirm": "Подтверждение бронирования",
            "quote": "Запрос цены / смета",
            "checkout": "Оформление заказа",
            "homepage": "Главная страница",
            "product": "Страницы продуктов",
            "location": "Локации",
            "pricing": "Цены и пакеты",
            "use_case": "Use cases / решения",
            "search_results": "Поиск / каталог",
            "about": "О компании",
            "general": "Прочие страницы",
        }
        section_label = type_labels.get(ptype, ptype)

        items_html = ""
        for r in pages:
            path = r.get("path", "")
            ctas = r.get("cta_elements", [])
            missing = r.get("missing_events", [])
            px = r.get("pixel_events", {})
            shopify_plats = r.get("shopify_pixel_platforms", [])

            display_name = "Главная страница" if path == "/" else path

            # Что зафиксировано при загрузке
            fired = []
            for plat, evts in px.items():
                names = [e["event"] for e in evts if not is_noise(plat, e["event"])]
                if names:
                    fired.append(f"{', '.join(names)} → {plat}")
                else:
                    noise_names = [e["event"] for e in evts if is_noise(plat, e["event"])]
                    if noise_names:
                        fired.append(f"PageView → {plat}")
            for plat in shopify_plats:
                if plat not in px:
                    fired.append(f"PageView → {plat}")

            fired_str = " &nbsp;|&nbsp; ".join(fired) if fired else "Ничего не зафиксировано"

            cta_str = ", ".join(ctas[:4]) if ctas else "—"

            missing_rows = ""
            for ev in missing:
                missing_rows += f'<div class="ev-missing">{ev}: не зафиксирован при загрузке</div>'

            items_html += f"""
            <div class="gap-card">
                <div class="gap-card-path">{display_name}</div>
                <div class="gap-card-row"><span class="gc-label">Кнопки на странице</span><span class="gc-val">{cta_str}</span></div>
                <div class="gap-card-row"><span class="gc-label">При загрузке</span><span class="gc-val dim">{fired_str}</span></div>
                {missing_rows}
            </div>"""

        gap_sections_html += f"""
        <div class="gap-section">
            <div class="gap-section-title">{section_label}</div>
            {items_html}
        </div>"""

    # ── Unverified: пиксель есть, событие не подтверждено ──────────
    unverified_items = ""
    for r in unverified_pages:
        u_path = r.get("path", "")
        u_name = "Главная страница" if u_path == "/" else u_path
        u_pids = r.get("pixel_ids", {})
        u_ids_str = " &nbsp;|&nbsp; ".join(f"{plat}: {', '.join(ids)}" for plat, ids in u_pids.items()) if u_pids else "—"
        u_reason = r.get("unverified_reason") or r.get("status", "")
        u_dup = ""
        if r.get("duplicate_pixels"):
            u_dupnames = ", ".join(r["duplicate_pixels"])
            u_dup = f'<div class="ev-missing" style="color:#d9a300">Дубль: {u_dupnames} — возможен двойной счёт</div>'
        unverified_items += f"""
        <div class="gap-card" style="border-left:3px solid #d9a300">
            <div class="gap-card-path">{u_name}</div>
            <div class="gap-card-row"><span class="gc-label">Пиксели по ID</span><span class="gc-val">{u_ids_str}</span></div>
            {u_dup}
            <div class="gap-card-row"><span class="gc-label dim">Статус</span><span class="gc-val dim">{u_reason}</span></div>
        </div>"""

    # ── Дубли пикселей (site-level) ────────────────────────────────
    dup_rows_html = ""
    for p in dup_pages:
        for plat in p.get("duplicate_pixels", []):
            ids = p.get("pixel_ids", {}).get(plat, [])
            ids_join = ", ".join(ids)
            dup_rows_html += f'<div class="ext-row"><div class="ext-svc">{plat}</div><div class="ext-note">{p.get("path", "")} — {len(ids)} ID: {ids_join}</div></div>'

    # ── Внешние сервисы ────────────────────────────────────────────
    ANALYTICS_TOOLS = {"Microsoft Clarity", "Hotjar", "FullStory"}
    conv_ext = {s: p for s, p in external_services.items() if s not in ANALYTICS_TOOLS}
    anal_ext = {s: p for s, p in external_services.items() if s in ANALYTICS_TOOLS}

    ext_rows = ""
    for svc, pages in conv_ext.items():
        pages_str = ", ".join(pages[:3])
        ext_rows += f"""
        <div class="ext-row">
            <div class="ext-svc">{svc}</div>
            <div class="ext-note">Конверсии происходят на стороне сервиса — пиксели их не видят</div>
            <div class="ext-pages">Найден на: {pages_str}</div>
        </div>"""

    anal_rows = ""
    for svc, pages in anal_ext.items():
        anal_rows += f'<div class="anal-row"><span class="anal-name">{svc}</span><span class="anal-note">Heatmap / session recording установлен</span></div>'

    # OK section
    if ok_pages:
        ok_items = "".join(
            f'<div class="ok-row"><span class="ok-path">{r.get("path","")}</span>' +
            f'<span class="ok-events">{", ".join(r.get("conversion_events_found",[])[:4])}</span></div>'
            for r in ok_pages
        )
        ok_section_html = f'''<div class="section">
    <div class="section-title">Трекинг настроен корректно</div>
    {ok_items}
  </div>'''
    else:
        ok_section_html = ""

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tracking Audit — {domain}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@300;400;500;600&display=swap');

  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

  :root {{
    --bg:      #0d0d12;
    --bg2:     #13131a;
    --bg3:     #1c1c26;
    --border:  #252530;
    --border2: #32323f;
    --text:    #e8e8f0;
    --dim:     #7a7a8a;
    --accent:  #5b7fff;
    --green:   #3ecf8e;
    --yellow:  #f5c842;
    --red:     #ff5c5c;
    --mono:    'DM Mono', monospace;
    --sans:    'DM Sans', sans-serif;
  }}

  body {{
    font-family: var(--sans);
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    min-height: 100vh;
  }}

  /* ── Header ── */
  .header {{
    border-bottom: 1px solid var(--border);
    padding: 28px 48px;
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
  }}
  .header-left {{ display: flex; flex-direction: column; gap: 4px; }}
  .header-label {{ font-family: var(--mono); font-size: 11px; color: var(--accent); letter-spacing: 0.15em; text-transform: uppercase; }}
  .header-domain {{ font-size: 22px; font-weight: 600; color: var(--text); margin-top: 2px; }}
  .header-right {{ display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }}
  .header-date {{ font-family: var(--mono); font-size: 12px; color: var(--dim); }}

  .verdict {{
    display: inline-block;
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    padding: 6px 14px;
    border-radius: 4px;
    font-weight: 500;
  }}
  .verdict.ok      {{ background: rgba(62,207,142,0.12); color: var(--green); border: 1px solid rgba(62,207,142,0.3); }}
  .verdict.warning {{ background: rgba(245,200,66,0.12); color: var(--yellow); border: 1px solid rgba(245,200,66,0.3); }}
  .verdict.critical{{ background: rgba(255,92,92,0.12);  color: var(--red);    border: 1px solid rgba(255,92,92,0.3); }}

  /* ── Stats ── */
  .stats {{
    display: grid;
    border-bottom: 1px solid var(--border);
  }}
  .stats-top {{
    grid-template-columns: repeat(2, 1fr);
    border-bottom: 1px solid var(--border);
  }}
  .stats-bottom {{
    grid-template-columns: repeat(3, 1fr);
    border-bottom: 1px solid var(--border);
  }}
  .stat {{
    padding: 32px 48px;
    border-right: 1px solid var(--border);
  }}
  .stat:last-child {{ border-right: none; }}
  .stat-num {{ font-family: var(--mono); font-size: 40px; font-weight: 500; line-height: 1; margin-bottom: 8px; }}
  .stat-num.ok  {{ color: var(--green); }}
  .stat-num.gap {{ color: var(--yellow); }}
  .stat-num.def {{ color: var(--accent); }}
  .stat-label {{ font-size: 12px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.08em; }}
  .stat-sub {{ font-size: 12px; color: var(--dim); margin-top: 4px; }}

  /* ── Sections ── */
  .content {{ padding: 0 48px 64px; }}
  .section {{ margin-top: 48px; }}
  .section-title {{
    font-family: var(--mono);
    font-size: 11px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
    color: var(--accent);
    margin-bottom: 16px;
    padding-bottom: 10px;
    border-bottom: 1px solid var(--border);
  }}

  /* ── Tool rows ── */
  .tool-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 20px;
    background: var(--bg2);
    border: 1px solid var(--border);
    margin-bottom: 4px;
    border-radius: 6px;
  }}
  .tool-label {{ font-size: 14px; color: var(--text); }}
  .tool-val {{ font-family: var(--mono); font-size: 13px; }}
  .tool-val.active  {{ color: var(--green); }}
  .tool-val.missing {{ color: var(--dim); }}

  /* ── Platform rows ── */
  .plat-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 14px 20px;
    background: var(--bg2);
    border: 1px solid var(--border);
    margin-bottom: 4px;
    border-radius: 6px;
  }}
  .plat-name {{ font-size: 14px; font-weight: 500; }}
  .plat-status {{ font-size: 13px; font-family: var(--mono); }}
  .plat-status.active  {{ color: var(--green); }}
  .plat-status.warning {{ color: var(--yellow); }}
  .plat-status.missing {{ color: var(--dim); }}

  /* ── GAP sections ── */
  .gap-section {{ margin-bottom: 32px; }}
  .gap-section-title {{
    font-size: 13px;
    font-weight: 600;
    color: var(--dim);
    text-transform: uppercase;
    letter-spacing: 0.08em;
    margin-bottom: 10px;
  }}
  .gap-card {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-left: 3px solid var(--yellow);
    border-radius: 6px;
    padding: 18px 22px;
    margin-bottom: 8px;
  }}
  .gap-card-path {{
    font-family: var(--mono);
    font-size: 14px;
    font-weight: 500;
    color: var(--text);
    margin-bottom: 12px;
  }}
  .gap-card-row {{
    display: flex;
    gap: 16px;
    margin-bottom: 6px;
    font-size: 13px;
  }}
  .gc-label {{ color: var(--dim); min-width: 140px; flex-shrink: 0; }}
  .gc-val   {{ color: var(--text); }}
  .gc-val.dim {{ color: var(--dim); }}
  .ev-missing {{
    font-family: var(--mono);
    font-size: 12px;
    color: var(--yellow);
    margin-top: 8px;
    padding: 6px 10px;
    background: rgba(245,200,66,0.07);
    border-radius: 4px;
    display: inline-block;
    margin-right: 6px;
  }}

  /* ── External services ── */
  .ext-row {{
    background: var(--bg2);
    border: 1px solid var(--border);
    border-left: 3px solid var(--red);
    border-radius: 6px;
    padding: 16px 20px;
    margin-bottom: 6px;
  }}
  .ext-svc   {{ font-size: 14px; font-weight: 600; margin-bottom: 4px; }}
  .ext-note  {{ font-size: 13px; color: var(--yellow); margin-bottom: 4px; }}
  .ext-pages {{ font-size: 12px; color: var(--dim); font-family: var(--mono); }}

  .anal-row {{
    display: flex;
    justify-content: space-between;
    padding: 12px 20px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-radius: 6px;
    margin-bottom: 4px;
    font-size: 13px;
  }}
  .anal-name {{ font-weight: 500; }}
  .anal-note {{ color: var(--dim); font-family: var(--mono); font-size: 12px; }}

  /* ── OK pages ── */
  .ok-row {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px 20px;
    background: var(--bg2);
    border: 1px solid var(--border);
    border-left: 3px solid var(--green);
    border-radius: 6px;
    margin-bottom: 4px;
  }}
  .ok-path   {{ font-family: var(--mono); font-size: 13px; }}
  .ok-events {{ font-size: 12px; color: var(--green); }}

  .empty-note {{ color: var(--dim); font-size: 13px; font-style: italic; padding: 16px 0; }}
</style>
</head>
<body>

<!-- Header -->
<div class="header">
  <div class="header-left">
    <div class="header-label">TNC · Tracking Audit</div>
    <div class="header-domain">{domain}</div>
  </div>
  <div class="header-right">
    <div class="header-date">{date_str}</div>
    <div class="verdict {verdict_cls}">{verdict_text}</div>
  </div>
</div>

<!-- Stats -->
<div class="stats stats-top">
  <div class="stat">
    <div class="stat-num def">{sitemap_total if sitemap_total else scanned}</div>
    <div class="stat-label">Страниц на сайте</div>
    <div class="stat-sub">{cats_str if cats_str else "обнаружено в sitemap"}</div>
  </div>
  <div class="stat">
    <div class="stat-num def">{scanned}</div>
    <div class="stat-label">Отобрано для аудита</div>
    <div class="stat-sub">{lang_sub}</div>
  </div>
</div>
<div class="stats stats-bottom">
  <div class="stat">
    <div class="stat-num {"ok" if pages_with_cta > 0 else "gap"}">{pages_with_cta} of {scanned}</div>
    <div class="stat-label">имеют CTA</div>
    <div class="stat-sub">кнопки и формы похожие на конверсии</div>
  </div>
  <div class="stat">
    <div class="stat-num {"ok" if pages_with_pixel > 0 else "gap"}">{pages_with_pixel} of {scanned}</div>
    <div class="stat-label">имеют пиксель</div>
    <div class="stat-sub">Meta, GA4, GTM или другие платформы</div>
  </div>
  <div class="stat">
    <div class="stat-num {"ok" if pages_with_conversion > 0 else "gap"}">{pages_with_conversion} of {scanned}</div>
    <div class="stat-label">имеют пиксель + событие</div>
    <div class="stat-sub">конверсионное событие зафиксировано</div>
  </div>
</div>

<div class="content">

  <!-- Google Tools -->
  <div class="section">
    <div class="section-title">Google — инструменты аналитики и рекламы</div>
    {gtm_row}
    {ga4_row}
    {ads_row}
  </div>

  <!-- Платформы -->
  <div class="section">
    <div class="section-title">Рекламные платформы</div>
    {plat_rows}
  </div>

  <!-- GAP страницы -->
  {"" if not gap_pages else f'''
  <div class="section">
    <div class="section-title">Страницы с пробелами — конверсионные события не зафиксированы при загрузке</div>
    {gap_sections_html}
  </div>
  '''}

  <!-- Unverified: пиксель есть, событие не подтверждено -->
  {"" if not unverified_pages else f'''
  <div class="section">
    <div class="section-title">⚠️ Пиксель установлен — срабатывание не подтверждено браузером</div>
    <p style="font-size:13px; color: var(--dim); margin-bottom: 16px;">
      Пиксель найден по ID, но конверсионное событие не зафиксировано в headless-скане
      (Meta-beacon в автоматическом браузере подавляется). Требуется проверка в обычном браузере — это не означает поломку.
    </p>
    {unverified_items}
  </div>
  '''}

  <!-- Дубли пикселей -->
  {"" if not dup_pages else f'''
  <div class="section">
    <div class="section-title">⚠️ Дублирующие пиксели — возможен двойной счёт</div>
    <p style="font-size:13px; color: var(--dim); margin-bottom: 16px;">
      Найдено более одного ID одной платформы. Частая причина искажённого ROAS.
      Проверить вручную (легитимный случай: пиксель агентства + пиксель клиента).
    </p>
    {dup_rows_html}
  </div>
  '''}

  <!-- NO TRACKING страницы -->
  {f'''
  <div class="section">
    <div class="section-title">❌ Пикселей нет вообще</div>
    <p style="font-size:13px; color: var(--dim); margin-bottom: 16px;">
      На этих страницах не обнаружено ни одного tracking пикселя. Attribution отсутствует полностью.
    </p>
    {"".join(
      f'<div class="ok-row"><span class="ok-path">{r.get("path","")}</span><span class="ok-events" style="color:var(--red,#e05)">Пикселей не найдено</span></div>'
      for r in data.get("no_tracking_pages", [])
    )}
  </div>
  ''' if data.get("no_tracking_pages") else ""}

  <!-- OK страницы -->
  {ok_section_html}

  <!-- Внешние сервисы -->
  {"" if not conv_ext else f'''
  <div class="section">
    <div class="section-title">Внешние сервисы — конверсии вне сайта</div>
    <p style="font-size:13px; color: var(--dim); margin-bottom: 16px;">
      Пользователи совершают действия в этих сервисах — пиксели их не видят. Attribution теряется.
    </p>
    {ext_rows}
  </div>
  '''}

  <!-- Аналитика -->
  {"" if not anal_ext else f'''
  <div class="section">
    <div class="section-title">Поведенческая аналитика</div>
    {anal_rows}
  </div>
  '''}

</div>
</body>
</html>"""

    return html


def run(step2_path: str):
    data = load_json(step2_path)
    step2_dir = os.path.dirname(step2_path)
    domain = urlparse(data.get("base_url", "")).netloc

    gtm_data = load_gtm(domain, step2_dir)
    step1_stats = load_step1_stats(domain, step2_dir)
    data["sitemap_total"] = step1_stats["total"]
    data["sitemap_categories"] = step1_stats["categories"]
    html = generate_html(data, gtm_data)

    out_path = os.path.join(step2_dir, f"{domain}_report.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"✅ HTML отчёт: {out_path}")
    try:
        webbrowser.open(f"file://{os.path.abspath(out_path)}")
    except Exception:
        pass

    return out_path


if __name__ == "__main__":
    from utils import setup_console
    setup_console()  # UTF-8 + ANSI на Windows (фикс cp1252-крэша при standalone-запуске)
    if len(sys.argv) < 2:
        print("Usage: python generate_report_html.py scans/domain/step2.json")
        sys.exit(1)
    run(sys.argv[1])
