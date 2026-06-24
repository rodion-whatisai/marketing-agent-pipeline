"""
03 · Execution — policy: переваренные сигналы → ОДНО действие + JSON audit.

Граница: решение — детерминированное правило (этот код). Где «судит» агент — помечено
agent_hook (сейчас детерминированный fallback). Агент НИКОГДА не удаляет: максимум
pause_candidate. Демо гоняется на заглушках (`python policy.py`), не на live-аккаунте.
"""
from __future__ import annotations
import json
from dataclasses import asdict
from schema import AdSet, CampaignState, Decision, AuditRecord
import meta_api
import signals


def agent_hook_explain(sigs: dict) -> str:
    """[АГЕНТ-ХУК] человекочитаемое «почему» для аудита/человека.
    Сейчас — детерминированный fallback; в проде здесь LLM на ГОТОВЫХ сигналах, не на сырье."""
    return "; ".join(s.verdict for s in sigs.values())


def decide(a: AdSet, c: CampaignState, sigs: dict) -> tuple[Decision, list]:
    """Сверху вниз: сначала состояние кампании и невиновность, потом сам ad set. Одно действие."""
    blockers = signals.innocence_check(sigs, a.anchor_in_stock)
    pacing = sigs["campaign_pacing"].value
    saturated = sigs["saturation"].value < 0.05

    if blockers:
        action = "send_to_human"
        why = f"kill заблокирован проверкой невиновности: {', '.join(blockers)} → ручной разбор"
    elif saturated:
        action = "pause_candidate"
        why = "выгорел (новый охват иссяк), внешних причин нет → кандидат на паузу (не delete)"
    elif a.cpa < c.kpi_cpa_target and a.roas >= 1.0:
        action = "scale"
        why = "эффективен и в KPI → поднять бюджет ≤20%/шаг"
    elif a.learning_status == "learning":
        action = "hold"
        why = "learning phase — не трогаем"
    elif pacing == "behind_over_cost":
        action = "reconcile_plan_vs_fact"
        why = "кампания не доставит в KPI → разговор план-vs-факт по позициям медиаплана"
    else:
        action = "do_nothing"
        why = "в графике, сигналов на действие нет"

    return Decision(a.id, action, why), blockers


def build_audit(a: AdSet, d: Decision, sigs: dict, blockers: list) -> AuditRecord:
    requires_human = d.action in ("send_to_human", "pause_candidate") or a.budget_day >= 500
    delta = 1.2 if d.action == "scale" else 1.0   # +20% при scale, иначе бюджет не трогаем
    return AuditRecord(
        adset_id=a.id,
        signals=[{"name": s.name, "value": s.value, "verdict": s.verdict} for s in sigs.values()],
        rule_fired=d.rationale,
        confidence=0.8 if not blockers else 0.5,
        blocked_by=blockers,
        required_approval=requires_human,
        budget_before=a.budget_day,
        budget_after=round(a.budget_day * delta, 2),
        rollback_state={"budget_day": a.budget_day, "status": "active"},  # храним прежнее → откат
    )


def run_on_stub(adset_id: str = "adset_D", campaign_id: str = "camp_1") -> dict:
    """Демо НА ЗАГЛУШКАХ (не live): Meta-стабы → digest → decide → audit."""
    a = meta_api.fetch_insights(adset_id)
    cfg = meta_api.fetch_adset_config(adset_id)
    snaps = meta_api.fetch_daily_reach(adset_id)
    c = meta_api.fetch_campaign(campaign_id)
    # медианы по портфелю — в проде из всех ad sets кампании; здесь фикс для демо
    sigs = signals.digest(a, cfg, snaps, c, ctr_median=1.9, cvr_median=2.4)
    d, blockers = decide(a, c, sigs)
    audit = build_audit(a, d, sigs, blockers)
    return {"decision": asdict(d), "audit": asdict(audit)}


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")   # Windows cp1252 → UTF-8 для кириллицы/стрелок
    print(json.dumps(run_on_stub(), ensure_ascii=False, indent=2))
