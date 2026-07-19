# -*- coding: utf-8 -*-
"""
HTML-репорт «Ads Library Intelligence v2» — режим одного домена (максимум).
Печатается из fb_audience_report (--html) по calc/records; standalone —
пересчитывает по готовым сайдкарам (--skip-deep логика).

Секции: методология сбора → выжимка (агрегаты) → Top-30 карточек с картинками →
все отсмотренные объявления (таблица) → демография по странам → каркас
«Прикидка бюджета». Язык: русский (перевод на EN — отдельным проходом позже).
Всё в одном самодостаточном файле (картинки base64 inline).

Standalone:
    python fb_audience_report_html.py client-a.example
"""
import base64
import html as htmllib
from datetime import date
from pathlib import Path

from utils import setup_console
setup_console()
from log import log_info, log_debug, log_success, log_step

TOP_CARDS = 30  # карточек с картинками в HTML (решение Rodion'а 2026-07-18)


def esc(s):
    return htmllib.escape(str(s)) if s is not None else ""


def _img_data_url(save_as: str, lib_id: str) -> str | None:
    p = Path("scans") / save_as / "fb_ads_images" / "active" / f"{lib_id}.jpg"
    if not p.exists():
        return None
    try:
        return "data:image/jpeg;base64," + base64.b64encode(p.read_bytes()).decode()
    except Exception as e:
        log_debug(f"_img_data_url: {lib_id}: {e}")
        return None


def _fmt(n):
    """Числа с пробелами-разделителями; пусто — прочерк."""
    if n in (None, ""):
        return "—"
    try:
        return f"{int(n):,}".replace(",", " ")
    except (ValueError, TypeError):
        return str(n)


# ─── Секции ─────────────────────────────────────────────────────────────────

def render_methodology(calc: dict, listing: dict, save_as: str) -> str:
    cv, n = calc["cover"], calc["n"]
    def pct(x):
        return f"{100 * x / n:.0f}%" if n else "0%"
    return f"""
<section class="card">
  <h2>Как собрана эта статистика</h2>
  <ul class="method">
    <li>Источник — публичная <b>Meta Ads Library</b> (facebook.com/ads/library):
        реестр всех действующих объявлений рекламодателя.</li>
    <li>Собраны <b>все активные объявления страницы</b> на дату среза; по каждому открыта
        карточка Ad Details и снята панель прозрачности (EU/UK transparency).</li>
    <li><b>Видимость охвата ограничена регуляторно:</b> Meta раскрывает охват только по
        Европейскому союзу и Великобритании (режимы прозрачности DSA/UK). Мировой охват
        коммерческих объявлений не публикуется — все цифры Reach ниже читаются как
        «сколько аккаунтов в EU / UK видели объявление хотя бы раз» (оценочная метрика).</li>
    <li>Таргетинг (гео/возраст/пол) — настройки рекламодателя; Reach и демография —
        фактическая доставка.</li>
    <li>«Зомби» в таблицах — техническое определение: {esc(calc['zombie_rule'])}.</li>
  </ul>
  <table class="mini">
    <tr><th>Покрытие данных</th><th>Доступно</th><th>Из</th></tr>
    <tr><td>Reach и демография (EU/UK)</td><td>{n}</td><td>{n}</td></tr>
    <tr><td>Плейсменты</td><td>{cv['placements']} ({pct(cv['placements'])})</td><td>{n}</td></tr>
    <tr><td>Формат креатива</td><td>{cv['format']} ({pct(cv['format'])})</td><td>{n}</td></tr>
    <tr><td>Дата старта</td><td>{cv['start_date']} ({pct(cv['start_date'])})</td><td>{n}</td></tr>
  </table>
  <p class="note">Часть параметров Ads Library отдаёт не для всех объявлений (дубли
     одного креатива группируются) — такие поля помечены «н/д».
     Данные собраны TNC, срез {date.today().isoformat()}.</p>
</section>"""


def render_summary(calc: dict) -> str:
    gt = calc["gender_tot"]
    T = sum(gt.values()) or 1
    ages = calc["age_by_gender"]
    core = max(["18-24", "25-34", "35-44", "45-54", "55-64", "65+"],
               key=lambda a: ages["male"].get(a, 0) + ages["female"].get(a, 0))
    eu_sum = sum((r["EU Reach"] or 0) if isinstance(r["EU Reach"], int) else 0
                 for r in calc["rows"])
    uk_sum = sum((r["UK Reach"] or 0) if isinstance(r["UK Reach"], int) else 0
                 for r in calc["rows"])
    fmt_c, pp_c, payer_c = {}, {}, {}
    for r in calc["rows"]:
        fmt_c[r["Формат"]] = fmt_c.get(r["Формат"], 0) + 1
        pp_c[r["Плейсменты"]] = pp_c.get(r["Плейсменты"], 0) + 1
        p = r["Плательщик"] or "—"
        payer_c[p] = payer_c.get(p, 0) + 1
    pp_known = {k: v for k, v in pp_c.items() if k != "no_data"}
    pp_line = "; ".join(f"{k}: {v}" for k, v in
                        sorted(pp_known.items(), key=lambda x: -x[1])) or "no_data"

    tiles = [
        ("Активных объявлений", _fmt(calc["n"])),
        ("EU Reach (сумма)", _fmt(eu_sum)),
        ("UK Reach (сумма)", _fmt(uk_sum)),
        ("Мужчины / Женщины", f"{100*gt['male']/T:.0f}% / {100*gt['female']/T:.0f}%"),
        ("Ядро аудитории", core),
        ("Форматы IMAGE / VIDEO", f"{fmt_c.get('IMAGE', 0)} / {fmt_c.get('VIDEO', 0)}"),
        ("Зомби", _fmt(calc["zombies"])),
    ]
    tiles_html = "\n".join(
        f'<div class="tile"><div class="tile-v">{esc(v)}</div>'
        f'<div class="tile-l">{esc(l)}</div></div>' for l, v in tiles)

    vert_rows = "\n".join(
        f"<tr><td>{esc(v)}</td><td class='r'>{a['n']}</td>"
        f"<td class='r'>{_fmt(a['eu'])}</td><td class='r'>{_fmt(a['uk'])}</td>"
        f"<td class='r'>{a['z']}</td>"
        f"<td class='r'>{a['fmt'].get('IMAGE', 0)}/{a['fmt'].get('VIDEO', 0)}</td></tr>"
        for v, a in sorted(calc["agg_v"].items(), key=lambda x: -x[1]["eu"]))

    age_rows = "\n".join(
        f"<tr><td>{a}</td><td class='r'>{_fmt(ages['male'].get(a, 0))}</td>"
        f"<td class='r'>{_fmt(ages['female'].get(a, 0))}</td></tr>"
        for a in ["18-24", "25-34", "35-44", "45-54", "55-64", "65+"])

    country_tot = {}
    for (c, _a), v in calc["country_agg"].items():
        country_tot[c] = country_tot.get(c, 0) + sum(v)
    top_c = sorted(country_tot.items(), key=lambda x: -x[1])[:10]
    country_rows = "\n".join(f"<tr><td>{esc(c)}</td><td class='r'>{_fmt(v)}</td></tr>"
                           for c, v in top_c)

    payer_line = "; ".join(f"{k}: {v}" for k, v in
                           sorted(payer_c.items(), key=lambda x: -x[1]) if k != "—")

    return f"""
<section class="card">
  <h2>Выжимка</h2>
  <div class="tiles">{tiles_html}</div>
  <p class="note">Плейсменты (по доступным): {esc(pp_line)}. Плательщики: {esc(payer_line)}.</p>
  <div class="cols3">
    <div><h3>Вертикали (по destination URL)</h3>
      <table class="mini"><tr><th>Вертикаль</th><th class="r">Объявл.</th><th class="r">EU Reach</th>
      <th class="r">UK Reach</th><th class="r">Зомби</th><th class="r">IMG/VID</th></tr>{vert_rows}</table></div>
    <div><h3>Возраст × пол (EU Reach)</h3>
      <table class="mini"><tr><th>Возраст</th><th class="r">Мужчины</th><th class="r">Женщины</th></tr>{age_rows}</table></div>
    <div><h3>Топ-10 стран (EU Reach)</h3>
      <table class="mini"><tr><th>Страна</th><th class="r">Reach</th></tr>{country_rows}</table></div>
  </div>
</section>"""


def _render_cards(rows, records, save_as, start_rank=1):
    cards = []
    for i, r in enumerate(rows, start_rank):
        lid = r["Library ID"]
        rec = records.get(lid) or {}
        img = _img_data_url(save_as, lid)
        img_html = (f'<img src="{img}" loading="lazy" alt="креатив">' if img
                    else '<div class="noimg">превью недоступно</div>')
        body = (rec.get("body_text") or "")[:220]
        return_bits = []
        if r["Формат"] != "no_data":
            return_bits.append(r["Формат"])
        if rec.get("video_preview_url") and not rec.get("image_url"):
            return_bits.append("видео-превью")
        fmt_note = " · ".join(return_bits)
        cards.append(f"""
<div class="ad-card">
  <div class="ad-rank">#{i}</div>
  {img_html}
  <div class="ad-body">
    <div class="ad-reach">EU Reach: <b>{_fmt(r['EU Reach'])}</b>
      &nbsp;·&nbsp; UK: {_fmt(r['UK Reach'])}</div>
    <div class="ad-text">{esc(body)}</div>
    <div class="ad-meta">{esc(r['Вертикаль'] or '—')} · CTA: {esc(r['CTA'] or '—')}
      · старт: {esc(r['Старт'] or '—')} {('· ' + esc(fmt_note)) if fmt_note else ''}</div>
    <div class="ad-meta"><a href="{esc(r['Ссылка'])}">открыть в Ads Library ↗</a>
      · ID {esc(lid)}</div>
  </div>
</div>""")
    return "\n".join(cards)


def render_top_cards(calc: dict, records: dict, save_as: str) -> str:
    """Две подгруппы: настоящий Top-10 по охвату EU (часть без превью — Ads Library
    не отдаёт их для карточек-групп) + примеры креативов с изображениями."""
    img_dir = Path("scans") / save_as / "fb_ads_images" / "active"
    by_reach = sorted([r for r in calc["rows"] if isinstance(r["EU Reach"], int)],
                      key=lambda r: -r["EU Reach"])
    top10 = by_reach[:10]
    top_ids = {r["Library ID"] for r in top10}
    visual = [r for r in by_reach
              if r["Library ID"] not in top_ids
              and (img_dir / f"{r['Library ID']}.jpg").exists()][:TOP_CARDS - 10]
    return f"""
<section class="card">
  <h2>Примеры объявлений</h2>
  <h3>Top-{len(top10)} по охвату EU</h3>
  <p class="note">Для части самых охватных объявлений Ads Library не отдаёт превью
     креатива (объявления-дубли группируются) — они показаны без изображения.</p>
  <div class="ads-grid">{_render_cards(top10, records, save_as)}</div>
  <h3>Примеры креативов</h3>
  <p class="note">Наиболее охватные объявления с доступным превью.</p>
  <div class="ads-grid">{_render_cards(visual, records, save_as, start_rank=11)}</div>
</section>"""


NUMERIC_COLS = {"EU Reach", "UK Reach", "%Муж"}
HIDDEN_COLS = {"Плательщик", "Бенефициар"}  # payer — ровно одно место, в выжимке


def _cell(v):
    return "н/д" if v == "no_data" else v


def render_full_table(calc: dict) -> str:
    if not calc["rows"]:
        return ""
    headers = [h for h in calc["rows"][0].keys() if h not in HIDDEN_COLS]
    head = "\n".join(
        f"<th class='r'>{esc(h)}</th>" if h in NUMERIC_COLS else f"<th>{esc(h)}</th>"
        for h in headers)
    body = "\n".join(
        "<tr>" + "\n".join(
            (f'<td><a href="{esc(r[h])}">открыть ↗</a></td>' if h == "Ссылка"
             else f"<td class='r'>{esc(_cell(r[h]))}</td>" if h in NUMERIC_COLS
             else f"<td>{esc(_cell(r[h]))}</td>") for h in headers) + "</tr>"
        for r in calc["rows"])
    return f"""
<section class="card">
  <details><summary><h2 class="inline">Приложение: все отсмотренные объявления ({calc['n']})</h2></summary>
  <div class="scroll"><table class="mini wide"><tr>{head}</tr>{body}</table></div>
  </details>
</section>"""


def render_country_demo(calc: dict) -> str:
    order = {a: i for i, a in enumerate(["18-24", "25-34", "35-44", "45-54", "55-64", "65+"])}
    tot_by_c = {}
    for (c, _a), v in calc["country_agg"].items():
        tot_by_c[c] = tot_by_c.get(c, 0) + sum(v)
    rows = "\n".join(
        f"<tr><td>{esc(c)}</td><td>{esc(a)}</td><td class='r'>{_fmt(v[0])}</td>"
        f"<td class='r'>{_fmt(v[1])}</td><td class='r'>{_fmt(v[2])}</td></tr>"
        for (c, a), v in sorted(calc["country_agg"].items(),
                                key=lambda x: (-tot_by_c[x[0][0]], x[0][0],
                                               order.get(x[0][1], 9))))
    return f"""
<section class="card">
  <details><summary><h2 class="inline">Демография по странам (EU, все объявления)</h2></summary>
  <div class="scroll"><table class="mini"><tr><th>Страна</th><th>Возраст</th>
  <th class="r">Мужчины</th><th class="r">Женщины</th><th class="r">Неизвестно</th></tr>{rows}</table></div>
  </details>
</section>"""


def render_budget_stub() -> str:
    return """
<section class="card budget">
  <h2>Прикидка бюджета</h2>
  <p class="note">Методика: охват EU+UK → оценка показов (коэффициент частоты) →
     CPM-бенчмарк по гео и платформе → оценка затрат. Раздел заполняется
     экспертной оценкой.</p>
</section>"""


def render_recommendations_stub() -> str:
    return """
<section class="card budget">
  <h2>Рекомендации</h2>
  <p class="note">Раздел заполняется экспертом по итогам данных выше.</p>
</section>"""


CSS = """
body{font:14px/1.5 'IBM Plex Sans','Segoe UI',sans-serif;margin:0;background:#f5f6f8;color:#1c1e21}
.wrap{max-width:1180px;margin:0 auto;padding:24px}
h1{margin:0;font-size:26px} h2{font-size:18px;margin:0 0 12px} h3{font-size:14px;margin:14px 0 6px}
h2.inline{display:inline;font-size:16px}
.subtitle{color:#606770;margin:4px 0 2px} .meta{color:#8a8d91;font-size:12px;margin-bottom:18px}
section.card{background:#fff;border-radius:10px;padding:18px 20px;margin-bottom:16px;
  box-shadow:0 1px 2px rgba(0,0,0,.06)}
ul.method{margin:0 0 12px;padding-left:20px} ul.method li{margin-bottom:6px}
.note{color:#606770;font-size:12px}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px}
.tile{background:#f0f2f5;border-radius:8px;padding:10px 16px;min-width:120px}
.tile-v{font-size:20px;font-weight:600} .tile-l{font-size:11px;color:#606770;margin-top:2px}
.cols3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px}
table.mini{border-collapse:collapse;font-size:12px;width:100%}
table.mini th{background:#f0f2f5;padding:5px 8px;text-align:left;border-bottom:1px solid #dde0e4}
table.mini td{padding:4px 8px;border-bottom:1px solid #eef0f2;vertical-align:top}
table.mini tr:nth-child(even) td{background:#f7f8fa}
td.r,th.r{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
table.wide{min-width:1400px} .scroll{overflow-x:auto;max-height:560px;overflow-y:auto}
.ads-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:14px}
.ad-card{border:1px solid #e4e6ea;border-radius:8px;overflow:hidden;position:relative;
  background:#fff;display:flex;flex-direction:column}
.ad-card img{width:100%;height:200px;object-fit:cover;background:#f0f2f5}
.noimg{width:100%;height:200px;display:flex;align-items:center;justify-content:center;
  background:#f0f2f5;color:#8a8d91;font-size:12px}
.ad-rank{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.65);color:#fff;
  font-size:11px;padding:2px 8px;border-radius:10px}
.ad-body{padding:10px 12px;font-size:12px}
.ad-reach{margin-bottom:6px} .ad-text{color:#1c1e21;margin-bottom:6px}
.ad-meta{color:#606770;font-size:11px;margin-top:2px}
.ad-meta a{color:#1877f2;text-decoration:none}
details summary{cursor:pointer;list-style-position:outside;margin-bottom:10px}
section.budget{border:1px dashed #c4c8cf}
footer{color:#8a8d91;font-size:11px;text-align:center;padding:10px 0 30px}
"""


def build_html(save_as: str, calc: dict, records: dict, listing: dict) -> Path:
    """Собирает «Ads Library Intelligence v2» по calc из fb_audience_report."""
    log_step("HTML-репорт «Ads Library Intelligence v2»", emoji="📄")
    page0 = (listing or {}).get("page") or {}
    handle = page0.get("handle") or ""
    display = page0.get("display_name") or save_as
    html_doc = f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>{esc(save_as)} — Ads Library Intelligence</title>
<style>{CSS}</style></head><body><div class="wrap">
<h1>{esc(save_as)}</h1>
<div class="subtitle">Facebook Ads Library — Intelligence Report ·
  {esc(display)}{(' (@' + esc(handle) + ')') if handle else ''}</div>
<div class="meta">Срез: {date.today().isoformat()} · собрано TNC · активных объявлений: {calc['n']}</div>
{render_methodology(calc, listing, save_as)}
{render_summary(calc)}
{render_budget_stub()}
{render_recommendations_stub()}
{render_top_cards(calc, records, save_as)}
{render_full_table(calc)}
{render_country_demo(calc)}
<footer>Подготовлено TNC · {date.today().isoformat()}</footer>
</div></body></html>"""
    out = Path("scans") / save_as / f"{save_as} — Ads Library Intelligence v2.html"
    out.write_text(html_doc, encoding="utf-8")
    log_success(f"репорт: {out} ({out.stat().st_size // 1024} КБ)")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="domain (данные должны быть прогнаны fb_audience_report)")
    ap.add_argument("--save-as", default=None)
    a = ap.parse_args()
    from fb_audience_report import run
    run(a.target, save_as=a.save_as, skip_deep=True, html=True)
