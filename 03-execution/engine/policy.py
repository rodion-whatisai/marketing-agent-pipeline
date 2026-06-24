"""
03 · Execution — policy: сигналы → ОДНО действие + маршрут (Python-resolver / human).

Порядок гейтов (зафиксирован):
  Gate 1 — невиновность  (атрибуция / сайт-диссонанс / сток; сайт-сток → сначала сканер 01)
  Gate 2 — learning      (learning → hold)
  Gate 3 — эффективность (scale эффективных при выбираемом бюджете; плохие → диагностика)

Агент НИКОГДА не удаляет: максимум pause_candidate. Демо на заглушках (`python policy.py`).
"""
from __future__ import annotations
import json
from dataclasses import asdict
from schema import AdSet, CampaignState, Decision, AuditRecord
import meta_api
import signals


def decide(a: AdSet, sigs: dict, c: CampaignState):
    """Возвращает (Decision, trace, route)."""
    trace = []

    # ── Gate 1 — невиновность ─────────────────────────────────────────────
    # Атрибуция ПЕРВОЙ: незрелое окно даёт CVR↓, который иначе ложно читается как
    # «проблема сайта» и уводит в site-скан до того, как мы вообще узнали правду.
    # Кейс D (ловушка атрибуции) обязан холдиться здесь, а не идти в сайт-диссонанс.
    if sigs["attribution"].value == "immature":
        trace.append("g1: атрибуция не дозрела → wait (до site-диагностики не доходим)")
        return (Decision(a.id, "hold", "attribution не дозрело — wait, не судим свежий спенд"),
                trace, "python (auto-wait)")
    # Только при ЗРЕЛОМ окне мягкий сайт-диссонанс (CTR↑/CVR↓) или сток → скан 01.
    if sigs["dissonance"].value == "site" or not a.anchor_in_stock:
        trace.append("g1: подозрение сайт/сток → прогон сканером 01")
        scan = signals.site_scan_stub(a)
        if scan["oos"] or scan["site_problem"]:
            return (Decision(a.id, "send_to_human",
                             "сайт/сток подтверждён сканером 01 → ручной разбор"),
                    trace, "human")
    trace.append("g1: невиновность чиста")

    # ── Gate 2 — learning ─────────────────────────────────────────────────
    if a.learning_status == "learning":
        trace.append("g2: learning")
        return (Decision(a.id, "hold", "learning phase — не трогаем (правка сбросит learning)"),
                trace, "python (auto-hold)")
    trace.append("g2: не learning")

    # ── Gate 3 — эффективность ────────────────────────────────────────────
    if (a.cpa <= c.kpi_cpa_target and sigs["utility"].value >= 1.0
            and sigs["budget"].value == "выбирает"):
        # RESERVED-гард (обязательный, rules.RESERVED): при CBO/Advantage+ бюджет живёт
        # на УРОВНЕ КАМПАНИИ — двигать бюджет ad set бесполезно (Meta перераспределит сама).
        # Scale ad-set-бюджета применим только при ABO.
        if getattr(c, "budget_mode", "ABO") == "CBO":
            return (Decision(a.id, "send_to_human",
                             "эффективен, но кампания на CBO → бюджет двигаем на уровне "
                             "кампании, ad-set scale неприменим"),
                    trace, "human (campaign-level)")
        return (Decision(a.id, "scale",
                         f"CPA ${a.cpa:.0f} ≤ target, utility {sigs['utility'].value}, "
                         f"бюджет выбирается → +20%"),
                trace, "python-resolver (авто в пределах cap)")
    if a.cpa > c.kpi_cpa_target:
        trace.append("g3: CPA выше target → диагностика причины")
        if sigs["dissonance"].value == "creative":
            return (Decision(a.id, "send_to_human",
                             "CTR↓ / CVR ок → проблема креатива: новые / выключить low-CTR объявление"),
                    trace, "human")
        if sigs["saturation"].value == "выгорел":
            return (Decision(a.id, "pause_candidate",
                             "выгорел по reach, чинить нечего → кандидат на паузу"),
                    trace, "human (approval)")
    return (Decision(a.id, "do_nothing", "сигналов на действие нет"), trace, "python (no-op)")


def build_audit(a: AdSet, d: Decision, sigs: dict, route: str) -> AuditRecord:
    delta = 1.2 if d.action == "scale" else 1.0
    return AuditRecord(
        adset_id=a.id,
        signals=[{"name": s.name, "value": s.value, "verdict": s.verdict} for s in sigs.values()],
        rule_fired=d.rationale,
        confidence=0.8,
        blocked_by=[] if d.action != "send_to_human" else [d.rationale],
        required_approval=("human" in route),
        budget_before=a.budget_day,
        budget_after=round(a.budget_day * delta, 2),
        rollback_state={"budget_day": a.budget_day, "status": "active"},
    )


def run_portfolio() -> list:
    """Прогон по всем ad set'ам портфеля (на заглушках). Возвращает решения + trace + audit."""
    port = meta_api.fetch_portfolio()
    c = meta_api.fetch_campaign("camp_1")
    total_spend = sum(a.spend for a in port)
    total_purch = sum(a.purchases for a in port)
    ctr_med = signals.median([a.ctr for a in port])
    cvr_med = signals.median([a.cvr for a in port])

    out = []
    for a in port:
        snaps = meta_api.fetch_daily_reach(a.id)
        sigs = signals.digest(a, snaps, ctr_med, cvr_med, total_spend, total_purch)
        d, trace, route = decide(a, sigs, c)
        out.append({
            "adset": a.name,
            "action": d.action,
            "route": route,
            "rationale": d.rationale,
            "trace": trace,
            "audit": asdict(build_audit(a, d, sigs, route)),
        })
    return out


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    for r in run_portfolio():
        print(f"\n=== {r['adset']} → {r['action'].upper()}  [{r['route']}]")
        print("   путь:", " · ".join(r["trace"]))
        print("   почему:", r["rationale"])
