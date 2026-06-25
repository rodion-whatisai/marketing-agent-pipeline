# -*- coding: utf-8 -*-
"""
03 · Execution — генератор формата **AttentionNeeded**.

Что делает: берёт портфель ad set'ов с метриками + бенчмарки по городам + 5-дневный
тренд CPL → переваривает в короткий приоритизированный список «что требует внимания и
почему». Этот список отдаётся **агенту или человеку** (он же — вход для гейтов policy-движка).

Принцип тот же, что в engine/: **Python переваривает сырьё → потребитель ест готовый
сигнал, а не 121 столбец таблицы.** Здесь не принимается необратимых действий — только
триаж и формулировка «почему». Решение scale/hold/pause — дальше, в policy.

Данные — анонимизированный срез реального FB lead-gen дашборда (7 городов). Имени клиента
нет; пороги (severity-бэнды) иллюстративные, настраиваются под клиента.

Запуск:
    python attention_needed.py            # демо на input/*.csv → JSON + Markdown + лог
"""
from __future__ import annotations
import csv, json, os, sys, io
from dataclasses import dataclass, asdict, field

# Windows-консоль: кириллица в cp1252 падает → принудительно utf-8
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))

# --- Пороги (ИЛЛЮСТРАТИВНЫЕ, настраиваются под клиента) -----------------------
# severity по отношению факт-CPL / бенчмарк-CPL:
BANDS = [
    (1.30, "high"),     # CPL ≥ +30% над нормой города
    (1.15, "medium"),   # +15…30%
    (1.00, "watch"),    # 0…15% над нормой — на контроле
]                       # < 1.00 → "ok" (дешевле нормы, внимания не требует)


@dataclass
class AttentionItem:
    """Одна строка формата AttentionNeeded — переваренный триаж по ad set'у."""
    ad_set: str
    city: str
    severity: str               # high | medium | watch | ok
    attention: bool             # severity != ok
    signal: str                 # cpl_over_benchmark | cpl_under_benchmark
    cpl_actual: float
    cpl_benchmark: float
    cpl_ratio: float            # факт / бенчмарк
    over_benchmark_pct: int     # (ratio-1)*100, округлённо
    cpl_trend_dir: str          # rising | falling | flat | n/a
    spent: float
    leads: int
    impressions_yesterday: int
    was_on_yesterday: bool
    rationale: str              # человекочитаемое «почему» (готовый сигнал)
    action_human_ref: str       # СПРАВОЧНО: что человек реально сделал (не рекомендация)
    cpl_trend_5d: list = field(default_factory=list)


# --- Переваривание -----------------------------------------------------------

def severity_for(ratio: float) -> str:
    for thr, label in BANDS:
        if ratio >= thr:
            return label
    return "ok"


def trend_dir(points: list) -> str:
    """Направление 5-дневного тренда CPL. Нули = дни без спенда (не CPL) → отбрасываем."""
    clean = [p for p in points if isinstance(p, (int, float)) and p > 0]
    if len(clean) < 4:
        return "n/a"
    first2 = sum(clean[:2]) / 2
    last2 = sum(clean[-2:]) / 2
    if last2 > first2 * 1.05:
        return "rising"
    if last2 < first2 * 0.95:
        return "falling"
    return "flat"


def build_rationale(ratio, cpl_a, cpl_b, tdir, sev) -> str:
    pct = round((ratio - 1) * 100)
    if sev == "ok":
        return f"CPL ${cpl_a:.2f} ниже бенчмарка ${cpl_b:.2f} ({pct}%) — в норме"
    sign = "+" if pct >= 0 else ""
    note = {"rising": "тренд 5д растёт — ухудшается",
            "falling": "тренд 5д падает — выправляется",
            "flat": "тренд 5д ровный",
            "n/a": "тренда нет (мало данных)"}[tdir]
    return f"CPL ${cpl_a:.2f} vs бенч ${cpl_b:.2f} ({sign}{pct}%); {note}"


def digest(perf_rows: list, trend_map: dict) -> list:
    items = []
    for r in perf_rows:
        cpl_a = float(r["cpl_actual"]); cpl_b = float(r["cpl_benchmark"])
        ratio = cpl_a / cpl_b if cpl_b else 0.0
        sev = severity_for(ratio)
        pts = trend_map.get(r["ad_set"], [])
        tdir = trend_dir(pts)
        items.append(AttentionItem(
            ad_set=r["ad_set"], city=r["city"], severity=sev,
            attention=(sev != "ok"),
            signal="cpl_over_benchmark" if ratio >= 1.0 else "cpl_under_benchmark",
            cpl_actual=round(cpl_a, 2), cpl_benchmark=round(cpl_b, 2),
            cpl_ratio=round(ratio, 3), over_benchmark_pct=round((ratio - 1) * 100),
            cpl_trend_dir=tdir,
            spent=float(r["spent"]), leads=int(r["leads"]),
            impressions_yesterday=int(r["impressions_yesterday"]),
            was_on_yesterday=(r["was_on_yesterday"].strip().lower() == "yes"),
            rationale=build_rationale(ratio, cpl_a, cpl_b, tdir, sev),
            action_human_ref=r.get("action_human_ref", "-"),
            cpl_trend_5d=pts,
        ))
    # сортировка: severity-вес, затем ratio убыв.
    rank = {"high": 0, "medium": 1, "watch": 2, "ok": 3}
    items.sort(key=lambda x: (rank[x.severity], -x.cpl_ratio))
    return items


# --- Ввод/вывод --------------------------------------------------------------

def load_inputs():
    perf = list(csv.DictReader(open(os.path.join(HERE, "input", "ad_set_performance.csv"), encoding="utf-8")))
    trend_map = {}
    for r in csv.DictReader(open(os.path.join(HERE, "input", "cpl_trend_5d.csv"), encoding="utf-8")):
        pts = []
        for k in ("d5", "d4", "d3", "d2", "d1", "today"):
            v = r.get(k, "")
            pts.append(float(v) if v not in ("", None) else None)
        trend_map[r["ad_set"]] = pts
    return perf, trend_map


def to_payload(items: list) -> dict:
    sev_counts = {}
    for it in items:
        sev_counts[it.severity] = sev_counts.get(it.severity, 0) + 1
    return {
        "format": "AttentionNeeded",
        "scope": "performance ad sets · CPL vs city benchmark",
        "source": "анонимизированный FB lead-gen дашборд (7 городов)",
        "thresholds": {"high": ">=1.30", "medium": ">=1.15", "watch": ">=1.00", "ok": "<1.00"},
        "n_total": len(items),
        "n_attention": sum(1 for it in items if it.attention),
        "severity_counts": sev_counts,
        "items": [asdict(it) for it in items],
    }


def render_md(items: list, payload: dict) -> str:
    L = []
    L.append("# AttentionNeeded — execution-триаж по ad set'ам\n")
    L.append(f"Источник: {payload['source']}. "
             f"Всего ad set: **{payload['n_total']}**, требуют внимания: "
             f"**{payload['n_attention']}** (severity != ok).\n")
    L.append("Пороги (факт CPL / бенчмарк города): "
             "`high ≥1.30 · medium ≥1.15 · watch ≥1.00 · ok <1.00`.\n")
    L.append("| # | severity | city | ad set | CPL факт | бенч | ratio | тренд 5д | «почему» | (ref) человек |")
    L.append("|---|---|---|---|--:|--:|--:|---|---|---|")
    for i, it in enumerate(items, 1):
        L.append(f"| {i} | **{it.severity}** | {it.city} | `{it.ad_set}` | "
                 f"${it.cpl_actual:.2f} | ${it.cpl_benchmark:.2f} | {it.cpl_ratio:.2f} | "
                 f"{it.cpl_trend_dir} | {it.rationale} | {it.action_human_ref} |")
    L.append("\n> `(ref) человек` — что в исходном дашборде сделал оператор. Это **контекст**, "
             "не рекомендация: в августовском срезе оператор сворачивал почти всё (конец месяца), "
             "поэтому `off` стоит даже у дешёвых ad set'ов. AttentionNeeded ранжирует по факту vs "
             "бенчмарк — воспроизводимо и без этого шума.\n")
    return "\n".join(L)


def main():
    print("[1/5] читаю портфель + бенчмарки по городам (input/ad_set_performance.csv)")
    perf, trend_map = load_inputs()
    print(f"      → {len(perf)} ad set, {len(set(r['city'] for r in perf))} городов")

    print("[2/5] считаю отношение факт-CPL / бенчмарк-CPL по каждому ad set")
    print("[3/5] читаю 5-дневный тренд CPL → направление (нули = дни без спенда, отбрасываю)")
    print("[4/5] присваиваю severity по бэндам (high/medium/watch/ok) + формулирую «почему»")
    items = digest(perf, trend_map)

    payload = to_payload(items)
    print(f"[5/5] триаж готов: {payload['n_attention']} из {payload['n_total']} требуют внимания "
          f"→ {payload['severity_counts']}")

    out_json = os.path.join(HERE, "attention_needed.sample.json")
    out_md = os.path.join(HERE, "attention_needed.sample.md")
    json.dump(payload, open(out_json, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    open(out_md, "w", encoding="utf-8").write(render_md(items, payload))
    print(f"      записал: {os.path.basename(out_json)} (агенту) + {os.path.basename(out_md)} (человеку)")

    print("\n=== AttentionNeeded (severity != ok) ===")
    print(f"{'sev':7s} {'city':10s} {'ad_set':28s} {'CPL':>6s} {'bench':>6s} {'ratio':>6s} {'trend':8s}  почему")
    for it in items:
        if not it.attention:
            continue
        print(f"{it.severity:7s} {it.city[:10]:10s} {it.ad_set[:28]:28s} "
              f"{it.cpl_actual:6.2f} {it.cpl_benchmark:6.2f} {it.cpl_ratio:6.2f} "
              f"{it.cpl_trend_dir:8s}  {it.rationale}")


if __name__ == "__main__":
    main()
