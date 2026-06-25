# -*- coding: utf-8 -*-
"""
03 · Execution — РОУТЕР по цели кампании (campaign objective).

Первое, что происходит в execution: смотрим **objective** кампании и направляем её в
нужный стрим. Разные цели — разные метрики, разная политика; их НЕЛЬЗЯ судить одним движком.

    OUTCOME_SALES / purchases   → performance-стрим → engine/            (CPA · ROAS · корзина)
    OUTCOME_LEADS               → lead-gen-стрим    → attention-needed/  (CPL vs бенчмарк города)
    awareness / reach / traffic / engagement → ВНЕ скоупа (помечаем, не молчим)

Роутер только направляет — дальше каждый стрим живёт своей логикой. «И от этого пляшем.»

Провенанс: оба стрима — Python-реконструкция реальной спредшит-операции (~20 вкладок),
где формулы заменяли агента. Эти формулы — детерминированная **арматура** конструкции
(см. README, секция «Провенанс»).

Запуск:
    python router.py        # демо: смешанный портфель кампаний → раскладка по стримам
"""
from __future__ import annotations
import sys, io, json
from dataclasses import dataclass, asdict

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass


# --- Карта objective → стрим (детерминированная: «арматура») -------------------
# Значения objective по Meta ODAX + легаси-алиасы (старые кабинеты ещё их отдают).
PERFORMANCE = {"OUTCOME_SALES", "CONVERSIONS", "PRODUCT_CATALOG_SALES", "PURCHASE"}
LEAD_GEN    = {"OUTCOME_LEADS", "LEAD_GENERATION"}
OUT_OF_SCOPE = {
    "OUTCOME_AWARENESS", "BRAND_AWARENESS", "REACH",
    "OUTCOME_TRAFFIC", "LINK_CLICKS",
    "OUTCOME_ENGAGEMENT", "POST_ENGAGEMENT", "VIDEO_VIEWS",
    "OUTCOME_APP_PROMOTION", "APP_INSTALLS",
}

STREAMS = {
    "performance": {"handler": "engine/policy.py",        "what": "CPA · ROAS · корзина · scale/kill/hold"},
    "lead_gen":    {"handler": "attention-needed/attention_needed.py", "what": "CPL vs бенчмарк города · триаж AttentionNeeded"},
    "out_of_scope":{"handler": "—",                        "what": "не performance и не lead-gen — execution не трогает"},
    "unknown":     {"handler": "—",                        "what": "objective не распознан — в очередь человеку"},
}


@dataclass
class Campaign:
    """Минимум для маршрутизации — роутер работает на уровне кампании, не ad set."""
    id: str
    name: str
    objective: str


def route(objective: str) -> str:
    """objective → имя стрима. Детерминированно, без суждения."""
    o = (objective or "").strip().upper()
    if o in PERFORMANCE:
        return "performance"
    if o in LEAD_GEN:
        return "lead_gen"
    if o in OUT_OF_SCOPE:
        return "out_of_scope"
    return "unknown"


def route_portfolio(campaigns: list) -> list:
    out = []
    for c in campaigns:
        stream = route(c.objective)
        out.append({
            "campaign": c.name, "objective": c.objective,
            "stream": stream, "handler": STREAMS[stream]["handler"],
        })
    return out


# --- Заглушка портфеля (смешанные цели) — реальные эндпоинты в engine/meta_api.py
def fetch_campaigns_stub() -> list:
    return [
        Campaign("c1", "Ecomm_Store_Purchases_Q3", "OUTCOME_SALES"),
        Campaign("c2", "Photo_Studio_Leads_7cities", "OUTCOME_LEADS"),   # → 18 ad set из attention-needed/
        Campaign("c3", "Legacy_Conversions_Catalog", "CONVERSIONS"),     # легаси-алиас performance
        Campaign("c4", "Legacy_LeadGen_Form", "LEAD_GENERATION"),        # легаси-алиас lead-gen
        Campaign("c5", "Brand_Awareness_Launch", "OUTCOME_AWARENESS"),   # вне скоупа
        Campaign("c6", "Reels_Engagement_Boost", "OUTCOME_ENGAGEMENT"),  # вне скоупа
        Campaign("c7", "Mystery_Objective", "OUTCOME_WHATEVER"),         # не распознан → человеку
    ]


def main():
    print("[1/3] беру портфель кампаний (заглушка; реально — Meta API, см. engine/meta_api.py)")
    campaigns = fetch_campaigns_stub()
    print(f"      → {len(campaigns)} кампаний")

    print("[2/3] читаю objective каждой → направляю в стрим (детерминированно)")
    routed = route_portfolio(campaigns)

    counts = {}
    for r in routed:
        counts[r["stream"]] = counts.get(r["stream"], 0) + 1
    print(f"[3/3] раскладка: {counts}")

    print("\n=== Маршрутизация по цели кампании ===")
    print(f"{'кампания':30s} {'objective':22s} {'стрим':13s} → handler")
    for r in routed:
        print(f"{r['campaign'][:30]:30s} {r['objective'][:22]:22s} {r['stream']:13s} → {r['handler']}")

    print("\nЛегенда стримов:")
    for name, meta in STREAMS.items():
        print(f"  {name:13s} {meta['what']}")

    json.dump({"streams": STREAMS, "routed": routed}, open("router.sample.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("\nзаписал router.sample.json")


# Tested: 2026-06-24 on stub-портфеле (7 кампаний) — 2 performance / 2 lead_gen / 2 out_of_scope / 1 unknown
if __name__ == "__main__":
    main()
