# -*- coding: utf-8 -*-
"""
Эталонный блок отчёта — «правда» о трекинге сайта двумя таблицами:
  1) Что установлено (системы/пиксели) — тип, ID, состояние.
  2) Что отслеживается по шагам воронки (события) — состояние на каждой платформе.

Каждая строка снабжена подсказкой «как для бабушки» (иконка «?», hover-тултип
+ родной title браузера как fallback). Никаких вердиктов/оценок — только сухая
правда: что мы СВОИМИ ГЛАЗАМИ увидели во время скана.

Единственный источник описаний/подсказок/воронки — этот файл (не плодим копии).
Данные: all_pages (load pixel_events + click_result) + gtm.json (ids_found,
conversion_events). Утверждено Rodion'ом 2026-07-14 (tinytronics debug).
"""
import html

import platforms as _p
_NOISE = _p.as_report_noise_events()


def esc(s):
    return html.escape(str(s))


def _is_shown_event(plat, ev):
    """Пустые/служебные не показываем; PageView и прочие содержательные — показываем."""
    return ev not in ("fired", "track", "unknown", "request_fired")


def _dedupe_ids(xs):
    """Убирает голый числовой ID, если есть тот же с префиксом (963953247 vs AW-963953247)."""
    xs = list(dict.fromkeys(xs))
    drop = set()
    for a in xs:
        if a and a[0].isdigit():
            for b in xs:
                if b != a and b.endswith(a) and not b[0].isdigit():
                    drop.add(a)
    return [x for x in xs if x not in drop]


# ─── Описания систем/пикселей (что это + подсказка) ──────────────────────────
PLATFORM_META = {
    "GTM": dict(
        name="Диспетчер тегов Google", note="(GTM)", kind="container",
        short="«Коробка», через которую на сайт подключают остальные теги. Сама ничего не считает — только раздаёт.",
        tip="Это как электрощиток в доме: сам ничего не «делает», но через него подключены все «розетки» — реклама и аналитика. Мы зашли на сайт и увидели, что этот щиток включается."),
    "Google Ads": dict(
        name="Реклама Google", note="(Google Ads)", kind="ads",
        short="Рекламный кабинет Google: отмечает, кто пришёл с рекламы Google и что делал.",
        tip="Это счётчик рекламы Google. Он замечает, что человек зашёл на сайт, чтобы Google понимал, какая реклама привела людей."),
    "Google Analytics": dict(
        name="Google Analytics 4", note="", kind="analytics",
        short="Аналитика: считает визиты и действия людей на сайте.",
        tip="Это счётчик посещаемости — сколько людей заходит и что они нажимают."),
    "Meta": dict(
        name="Пиксель Meta", note="", kind="social",
        short="Трекинг рекламы Facebook / Instagram.",
        tip="Это счётчик рекламы Facebook и Instagram."),
    "TikTok": dict(
        name="Пиксель TikTok", note="", kind="social",
        short="Трекинг рекламы TikTok.",
        tip="Это счётчик рекламы TikTok."),
    "Pinterest": dict(
        name="Тег Pinterest", note="", kind="social",
        short="Трекинг рекламы Pinterest.",
        tip="Это счётчик рекламы Pinterest."),
    "Snapchat": dict(
        name="Пиксель Snapchat", note="", kind="social",
        short="Трекинг рекламы Snapchat.",
        tip="Это счётчик рекламы Snapchat."),
    "Bing/Microsoft": dict(
        name="Реклама Microsoft (Bing)", note="", kind="ads",
        short="Трекинг рекламы Microsoft / Bing.",
        tip="Это счётчик рекламы Microsoft (Bing)."),
    "LinkedIn": dict(
        name="Пиксель LinkedIn", note="", kind="social",
        short="Трекинг рекламы LinkedIn.",
        tip="Это счётчик рекламы LinkedIn."),
}

# Ядро показываем всегда (в т.ч. «Не найден» — клиенту важно видеть, чего НЕТ).
CORE_ALWAYS = ["GTM", "Google Ads", "Google Analytics", "Meta", "TikTok"]
EXTRA_IF_PRESENT = ["Pinterest", "Snapchat", "Bing/Microsoft", "LinkedIn"]

# Колонки воронки — платформы, у которых есть клиентские события-шаги
FUNNEL_COLS = ["Google Analytics", "Google Ads", "Meta", "TikTok", "Pinterest"]
FUNNEL_COL_LABEL = {
    "Google Analytics": "Google Analytics 4", "Google Ads": "Реклама Google",
    "Meta": "Meta", "TikTok": "TikTok", "Pinterest": "Pinterest",
}

# ─── Шаги воронки (события) ──────────────────────────────────────────────────
# reach=False → триггер недостижим автосканом (checkout/оплата). form=True →
# сработает только на реальном сабмите, а мы формы не отправляем (политика).
FUNNEL = [
    dict(label="Просмотр страницы", reach=True, form=False,
         tip="Самый простой шаг: человек открыл страницу.",
         syn={"Google Analytics": ["page_view"], "Google Ads": ["page_view"],
              "Meta": ["pageview"], "TikTok": ["pageview", "landingpageview"],
              "Pinterest": ["pagevisit"]}),
    dict(label="Просмотр товара", reach=True, form=False,
         tip="Человек открыл карточку товара. Если мы сами товар не открывали во время проверки — только «настроено», не «работает».",
         syn={"Google Analytics": ["view_item"], "Meta": ["viewcontent"],
              "TikTok": ["viewcontent"], "Pinterest": ["viewcategory"]}),
    dict(label="Добавление в корзину", reach=True, form=False,
         tip="Человек положил товар в корзину. Мы сами нажимаем «в корзину» — если счётчик поймал, значит работает.",
         syn={"Google Analytics": ["add_to_cart"], "Meta": ["addtocart"],
              "TikTok": ["addtocart"], "Pinterest": ["addtocart"]}),
    dict(label="Начало оформления", reach=False, form=False,
         tip="Человек начал оформлять заказ. Чтобы проверить, надо реально пойти в оформление — сканер туда не заходит.",
         syn={"Google Analytics": ["begin_checkout"], "Meta": ["initiatecheckout"],
              "TikTok": ["initiatecheckout"], "Pinterest": ["checkout"]}),
    dict(label="Покупка", reach=False, form=False,
         tip="Человек оплатил заказ. Чтобы проверить, нужна настоящая оплата картой — мы этого не делаем.",
         syn={"Google Analytics": ["purchase"], "Meta": ["purchase"],
              "TikTok": ["purchase", "placeanorder"]}),
    dict(label="Отправка заявки (форма)", reach=True, form=True,
         tip="Человек отправил форму-заявку. Чтобы проверить, надо реально отправить форму — мы формы не отправляем, чтобы не спамить владельца.",
         syn={"Google Ads": ["conversion"], "Google Analytics": ["generate_lead", "form_submit"],
              "Meta": ["lead"], "TikTok": ["lead", "submitform", "contact"]}),
]

STATES_LEGEND = [
    ("Работает — видели при заходе", "зашли на сайт, тег сработал сам, без действий. Самое надёжное «работает»."),
    ("Работает — подтвердили по действию", "тег сработал после того, как мы нажали кнопку. Живой, срабатывает на действие."),
    ("Найдено в настройках, запрос не видели", "тег есть в настройках сайта (GTM), но своими глазами срабатывание мы не поймали."),
    ("Настроено, не проверяли", "тег стоит, но повода сработать мы не создавали (не открыли товар и т.п.). Стоит — да; работает ли — не подтверждаем."),
    ("Проверить нельзя", "чтобы тег сработал, нужна реальная покупка/оплата или отправка заявки, а мы этого не делаем."),
    ("Не найдено на сайте", "такого пикселя/тега/события на сайте нет."),
]


# ─── Сбор фактов из данных скана ─────────────────────────────────────────────
def _facts(all_pages, gtm_data):
    load_pairs, click_pairs = set(), set()
    network_plats, shopify_plats, id_plats = set(), set(), set()
    ids = {}
    dup = set()
    gtmjs_loaded = False
    for p in all_pages or []:
        for pl, evs in (p.get("pixel_events") or {}).items():
            network_plats.add(pl)
            for e in evs:
                load_pairs.add((pl, str(e.get("event", "")).lower()))
        for pl, i in (p.get("pixel_ids") or {}).items():
            id_plats.add(pl)
            ids.setdefault(pl, set()).update(i)
        for pl in (p.get("shopify_pixel_platforms") or []):
            shopify_plats.add(pl)
        for pl in (p.get("duplicate_pixels") or []):
            dup.add(pl)
        cr = p.get("click_result") or {}
        for b in (cr.get("buttons") or []):
            for ev in (b.get("conversion_events") or []) + (b.get("partial_events") or []) + (b.get("events_fired") or []):
                if ":" in ev:
                    pl, name = ev.split(":", 1)
                    click_pairs.add((pl, name.strip().lower()))
        for u in (p.get("network_requests") or []):
            if "googletagmanager.com/gtm.js" in str(u):
                gtmjs_loaded = True

    # gtm.json: ids_found + conversion_events + сам ключ-контейнер (GTM-xxx)
    gtm_ids = {"GTM": set(), "Google Ads": set(), "Google Analytics": set()}
    ga4_conf = set()
    for cid, container in (gtm_data or {}).items():
        if str(cid).startswith("GTM-"):
            gtm_ids["GTM"].add(cid)          # GT-xxx (Google Tag) — НЕ GTM-контейнер, намеренно не берём
        idf = container.get("ids_found", {}) or {}
        gtm_ids["GTM"].update(idf.get("GTM", []))
        gtm_ids["Google Ads"].update(idf.get("Google Ads", []))
        gtm_ids["Google Analytics"].update(idf.get("GA4", []))
        for e in (container.get("conversion_events", {}) or {}).get("GA4", []):
            ga4_conf.add(str(e).lower())
    for pl in ("GTM", "Google Ads", "Google Analytics"):
        if gtm_ids[pl]:
            ids.setdefault(pl, set()).update(gtm_ids[pl])
            id_plats.add(pl)

    ids = {k: _dedupe_ids(sorted(v)) for k, v in ids.items()}
    # дубль = помечен сканером ИЛИ >1 различного ID у одной платформы
    for pl, xs in ids.items():
        if len(xs) > 1:
            dup.add(pl)

    present = network_plats | shopify_plats | id_plats | {p for p in gtm_ids if gtm_ids[p]}
    if gtm_ids["GTM"] or gtmjs_loaded:
        present.add("GTM")

    return dict(load=load_pairs, click=click_pairs, network=network_plats,
                present=present, ids=ids, dup=dup,
                ga4_conf=ga4_conf, gtmjs=gtmjs_loaded)


# ─── Состояния ───────────────────────────────────────────────────────────────
def _system_state(plat, f):
    if plat == "GTM":
        if f["gtmjs"]:
            return "load", "Загружается при заходе — видели вживую"
        if plat in f["present"]:
            return "cfg", "Найден в настройках, загрузку не видели"
        return "no", "Не найден на сайте"
    if plat in f["network"]:
        return "load", "Работает — видели при заходе"
    if any(pl == plat for pl, _ in f["click"]):
        return "click", "Работает — подтвердили по действию"
    if plat in f["present"]:
        return "cfg", "Найдено в настройках (GTM), запрос не видели"
    return "no", "Не найден на сайте"


def _cell_state(plat, step, f):
    syn = step["syn"].get(plat)
    if not syn:
        return "na", "—"
    if any((plat, s) in f["load"] for s in syn):
        return "load", "Работает — видели при заходе"
    if any((plat, s) in f["click"] for s in syn):
        return "click", "Сработало по действию"
    if step["form"]:
        return "mid", "Проверить нельзя — форму не отправляем"
    if not step["reach"]:
        return "mid", "Проверить нельзя — до оформления/оплаты не доходим"
    # достижимо, применимо, но не наблюдали
    if plat == "Google Analytics" and any(s in f["ga4_conf"] for s in syn):
        return "mid", "Настроено, не проверяли"
    if plat in f["present"]:
        return "no", "Не зафиксировано"
    return "na", "—"


# ─── Рендер ──────────────────────────────────────────────────────────────────
def _q(tip):
    t = esc(tip)
    return (f'<span class="rt-q" tabindex="0" title="{t}">?'
            f'<span class="rt-tip">{t}</span></span>')


def _fmt_ids(plat, f):
    xs = f["ids"].get(plat, [])
    return ", ".join(esc(x) for x in xs) if xs else "—"


def render(all_pages, gtm_data, domain=""):
    f = _facts(all_pages, gtm_data)
    st_cls = {"load": "rt-ok", "click": "rt-ok", "cfg": "rt-mid",
              "mid": "rt-mid", "no": "rt-no", "na": "rt-dash"}

    # ── Таблица 1: системы ──
    rows = list(CORE_ALWAYS) + [p for p in EXTRA_IF_PRESENT if p in f["present"]]
    sys_rows = ""
    for plat in rows:
        m = PLATFORM_META[plat]
        cls, label = _system_state(plat, f)
        note = f' <span class="rt-mut">{esc(m["note"])}</span>' if m["note"] else ""
        id_html = _fmt_ids(plat, f)
        if plat in f["dup"] and plat in f["present"]:
            n = len(f["ids"].get(plat, []))
            id_html += f' <span class="rt-dup" title="Найдено больше одного ID одной системы — возможен двойной счёт событий.">ДУБЛЬ{f" ×{n}" if n > 1 else ""}</span>'
        sys_rows += (
            f'<tr>'
            f'<td class="rt-qc">{_q(m["tip"])}</td>'
            f'<td class="rt-name">{esc(m["name"])}{note}</td>'
            f'<td>{esc(m["short"])}</td>'
            f'<td class="rt-id">{id_html}</td>'
            f'<td class="{st_cls[cls]}">{esc(label)}</td>'
            f'</tr>')
    sys_table = (
        '<table class="rt-tbl"><thead><tr><th></th><th>Что стоит</th>'
        '<th>Что это</th><th>Номер (ID)</th><th>Состояние</th></tr></thead>'
        f'<tbody>{sys_rows}</tbody></table>')

    # ── Таблица 2: воронка ──
    cols = [c for c in FUNNEL_COLS if c in f["present"]]
    if cols:
        head = "".join(f"<th>{esc(FUNNEL_COL_LABEL[c])}</th>" for c in cols)
        body = ""
        for step in FUNNEL:
            tds = ""
            for c in cols:
                cls, label = _cell_state(c, step, f)
                tds += f'<td class="{st_cls[cls]}">{esc(label)}</td>'
            body += (f'<tr><td class="rt-qc">{_q(step["tip"])}</td>'
                     f'<td class="rt-name">{esc(step["label"])}</td>{tds}</tr>')
        funnel_table = (
            f'<table class="rt-tbl"><thead><tr><th></th><th>Шаг воронки</th>{head}</tr>'
            f'</thead><tbody>{body}</tbody></table>')
        funnel_block = (f'<h2 class="rt-h2">Что отслеживается по шагам воронки</h2>{funnel_table}')
    else:
        funnel_block = ('<h2 class="rt-h2">Что отслеживается по шагам воронки</h2>'
                        '<p class="rt-empty">Пикселей с воронкой событий на сайте не найдено — отслеживать нечего.</p>')

    legend = "".join(
        f'<li><b>{esc(name)}</b> — {esc(desc)}</li>' for name, desc in STATES_LEGEND)

    return f'''<section class="rt">
  <h2 class="rt-h2">Что установлено на сайте</h2>
  {sys_table}
  {funnel_block}
  <h2 class="rt-h2">Что значит каждое состояние</h2>
  <ul class="rt-legend">{legend}</ul>
</section>
{CSS}'''


def _not_read(page) -> str:
    """Страница, которую мы не прочитали: пусто в её строках — это про НАШ заход,
    а не про отсутствие трекинга. Возвращает пояснение или "" если страница прочитана.
    Про сайт ничего не утверждаем: только «не дочитали» и код, который увидели."""
    gate = page.get("gate") or {}
    if gate.get("http_error"):
        code = gate.get("http_status")
        return f"не дочитали (HTTP {code})" if code else "не дочитали"
    if gate.get("redirected"):
        return "увела на другой адрес"
    return ""


def render_pages(all_pages):
    """Постраничный срез — из чего схлопнуты таблицы выше. Факт, без вердиктов."""
    rows = ""
    n_not_read = 0
    for p in all_pages or []:
        path = "Главная страница" if p.get("path") == "/" else esc(p.get("path", ""))
        skipped = _not_read(p)
        if skipped:
            # Прочерк тут означал бы «трекинга нет» — а мы просто не смотрели.
            n_not_read += 1
            rows += (f'<tr><td class="rt-name">{path}</td>'
                     f'<td class="rt-mut">{esc(p.get("page_type", ""))}</td>'
                     f'<td class="rt-mut" colspan="2">{esc(skipped)} — не проверялась</td></tr>')
            continue
        load = []
        for pl, evs in (p.get("pixel_events") or {}).items():
            names = sorted({e["event"] for e in evs if _is_shown_event(pl, e.get("event", ""))})
            if names:
                load.append(f'{esc(pl)}: {esc(", ".join(names))}')
        clicks = []
        for b in ((p.get("click_result") or {}).get("buttons") or []):
            clicks += (b.get("conversion_events") or [])
        load_str = " · ".join(load) if load else "—"
        click_str = esc(", ".join(sorted(set(clicks)))) if clicks else "—"
        rows += (f'<tr><td class="rt-name">{path}</td>'
                 f'<td class="rt-mut">{esc(p.get("page_type", ""))}</td>'
                 f'<td class="rt-ok">{load_str}</td>'
                 f'<td class="rt-ok">{click_str}</td></tr>')
    n = len(all_pages or [])
    # Охват раскрываем всегда, когда он неполный: иначе «не найдено на сайте» выше
    # читается как проверенный факт, хотя часть страниц мы не открывали.
    coverage = ""
    if n_not_read:
        coverage = (f'<p class="rt-mut">Проверено {n - n_not_read} из {n} страниц. '
                    f'{n_not_read} не дочитали — выводы выше построены без них.</p>')
    return (f'<section class="rt"><details class="rt-pg"><summary>Постранично — из чего собраны таблицы ({n} стр.)</summary>'
            f'{coverage}'
            f'<table class="rt-tbl"><thead><tr><th>Страница</th><th>Тип</th>'
            f'<th>События при загрузке</th><th>Конверсии по клику</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></details></section>')


CSS = '''<style>
.rt { font-family:"IBM Plex Sans",-apple-system,Segoe UI,Roboto,sans-serif; color:#d7dae0; }
.rt-h2 { font-size:13px; text-transform:uppercase; letter-spacing:.6px; color:#8a909c;
  font-weight:600; margin:26px 0 10px; border-bottom:1px solid #23262e; padding-bottom:6px; }
.rt-tbl { border-collapse:collapse; width:100%; font-size:13.5px; }
.rt-tbl th, .rt-tbl td { text-align:left; padding:10px 12px; vertical-align:top; border-bottom:1px solid #1c1f27; }
.rt-tbl thead th { color:#7d838f; font-weight:500; font-size:11.5px; text-transform:uppercase; letter-spacing:.4px; border-bottom:1px solid #2b2f3a; }
.rt-name { color:#eef0f3; font-weight:600; white-space:nowrap; }
.rt-mut { color:#7d838f; font-weight:400; }
.rt-id { font-family:"IBM Plex Mono",ui-monospace,monospace; color:#aeb4bf; font-size:12.5px; white-space:nowrap; }
.rt-ok { color:#cfd4dc; }
.rt-mid { color:#9aa0ab; }
.rt-no, .rt-dash { color:#6c717c; }
.rt-qc { width:26px; padding-right:0; }
.rt-empty { color:#9aa0ab; font-size:13px; }
.rt-legend { list-style:none; padding:0; margin:0; font-size:13px; }
.rt-legend li { padding:6px 0; border-bottom:1px solid #1c1f27; color:#aeb4bf; }
.rt-legend b { color:#dfe3e9; }
.rt-dup { display:inline-block; margin-left:6px; padding:1px 6px; border-radius:4px;
  background:#3a2a12; border:1px solid #6a4a1a; color:#f0b45a; font-size:10.5px;
  font-weight:700; font-family:"IBM Plex Sans",sans-serif; letter-spacing:.3px; }
.rt-pg summary { cursor:pointer; color:#8a909c; font-size:12.5px; padding:4px 0; }
.rt-pg summary:hover { color:#cfd4dc; }
.rt-pg table { margin-top:10px; font-size:12px; }
.rt-q { display:inline-flex; align-items:center; justify-content:center; width:17px; height:17px;
  border:1px solid #3a3f4b; border-radius:50%; color:#8a909c; font-size:11px; font-weight:700;
  cursor:help; position:relative; user-select:none; }
.rt-q:hover, .rt-q:focus { border-color:#5b6270; color:#cfd4dc; outline:none; }
.rt-q .rt-tip { display:none; position:absolute; left:22px; top:-4px; z-index:30; width:290px;
  background:#1b1e26; border:1px solid #333846; border-radius:8px; padding:10px 12px;
  color:#d7dae0; font-size:12.5px; font-weight:400; line-height:1.45; text-transform:none;
  letter-spacing:0; box-shadow:0 8px 24px rgba(0,0,0,.5); white-space:normal; }
.rt-q:hover .rt-tip, .rt-q:focus .rt-tip { display:block; }
</style>'''


# ─── Автономный прогон для проверки: python report_truth.py <domain> ─────────
if __name__ == "__main__":
    import sys, json, os
    sys.stdout.reconfigure(encoding="utf-8")
    dom = sys.argv[1] if len(sys.argv) > 1 else "tinytronics.nl"
    base = os.path.join("scans", dom)
    with open(os.path.join(base, f"{dom}_step2.json"), encoding="utf-8") as fh:
        data = json.load(fh)
    gtm = {}
    gp = os.path.join(base, "gtm.json")
    if os.path.exists(gp):
        with open(gp, encoding="utf-8") as fh:
            gtm = json.load(fh)
    body = render(data.get("all_pages", []), gtm, dom)
    pages = render_pages(data.get("all_pages", []))
    out = os.path.join(base, f"{dom}_truth_preview.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(f'<div style="background:#0e0f13;padding:24px;max-width:940px;margin:auto">'
                 f'<h1 style="font-family:sans-serif;color:#fff">{dom}</h1>{body}{pages}</div>')
    print("OK ->", out)
