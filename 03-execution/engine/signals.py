"""
03 · Execution — формирование сигналов (Python переваривает, агент ест готовое).

Чистые детерминированные функции. LLM сырые цифры читают плохо — поэтому здесь сырые
данные превращаются в короткие переваренные сигналы (verdict'ы), и уже ИХ кормят агенту.
"""
from schema import AdSet, AdSetConfig, ReachSnapshot, CampaignState, Signal


def incremental_reach(snaps: list[ReachSnapshot]) -> list[int]:
    """ДОПОЛНИТЕЛЬНЫЙ охват день-к-дню (НЕ cumulative): сколько НОВЫХ людей дал Facebook."""
    out, prev = [], 0
    for s in snaps:
        out.append(s.cumulative_reach - prev)
        prev = s.cumulative_reach
    return out


def saturation_signal(snaps: list[ReachSnapshot]) -> Signal:
    """Новый охват → ~0, а частота не падает → ad set выгорел (дохлый номер)."""
    inc = incremental_reach(snaps)
    last3 = inc[-3:] if len(inc) >= 3 else inc
    avg_new = sum(last3) / len(last3) if last3 else 0
    first_new = inc[0] if inc else 1
    ratio = (avg_new / first_new) if first_new else 0.0
    saturated = ratio < 0.05 and snaps[-1].frequency >= snaps[0].frequency
    return Signal("saturation", round(ratio, 3),
                  "выгорел: новый охват иссяк" if saturated else "охват ещё растёт")


def attribution_check(a: AdSet) -> Signal:
    """Свежий спенд + большой лаг → конверсии не дозрели, цифрам Meta верить рано."""
    immature = a.attribution_delay_days >= 5 and a.purchases < a.backend_purchases
    return Signal("attribution", a.attribution_delay_days,
                  "не дозрело: backend уже выше Meta" if immature else "окно закрыто, верим")


def dissonance_signal(a: AdSet, ctr_median: float, cvr_median: float) -> Signal:
    """Верх по CTR + низ по CVR → проблема НЕ в рекламе (сайт / оффер)."""
    diss = a.ctr >= ctr_median and a.cvr < cvr_median
    return Signal("dissonance", diss,
                  "диссонанс CTR↑/CVR↓ → сайт/оффер" if diss else "клик↔конверсия согласованы")


def offer_signal(a: AdSet) -> Signal:
    """Backend: высокие возвраты / низкий LTV → проблема ОФФЕРА, не рекламы."""
    refund_rate = a.refunds / max(a.backend_purchases, 1)
    bad = refund_rate > 0.15 or a.ltv < a.cpa
    return Signal("offer", round(refund_rate, 2),
                  "оффер слабый: refunds↑ / LTV↓" if bad else "оффер ок")


def campaign_pacing(c: CampaignState) -> Signal:
    """Чистая калькуляция: отстаёт-в-KPI / отстаёт-дороже / оверделивер / в графике."""
    time_frac = c.days_elapsed / c.days_total
    result_frac = c.actual_result / max(c.planned_result, 1)
    blended_cpa = c.actual_spend / max(c.actual_result, 1)
    if result_frac >= time_frac * 1.1:
        state = "overdelivering"
    elif result_frac < time_frac * 0.9:
        state = "behind_in_kpi" if blended_cpa <= c.kpi_cpa_target else "behind_over_cost"
    else:
        state = "on_track"
    return Signal("campaign_pacing", state,
                  f"{state}: факт {result_frac:.0%} результата за {time_frac:.0%} времени, "
                  f"CPA ${blended_cpa:.0f} vs target ${c.kpi_cpa_target:.0f}")


def innocence_check(sigs: dict, anchor_in_stock: bool) -> list:
    """«Проверка невиновности»: внешние причины, при которых kill ЗАПРЕЩЁН."""
    blockers = []
    if "не дозрело" in sigs["attribution"].verdict:
        blockers.append("attribution_lag")
    if "сайт/оффер" in sigs["dissonance"].verdict:
        blockers.append("site_or_offer")
    if "слабый" in sigs["offer"].verdict:
        blockers.append("weak_offer")
    if not anchor_in_stock:
        blockers.append("anchor_out_of_stock")
    return blockers


def digest(a: AdSet, cfg: AdSetConfig, snaps: list[ReachSnapshot],
           c: CampaignState, ctr_median: float, cvr_median: float) -> dict:
    """Всё сырьё → словарь переваренных сигналов. ЭТО кормим агенту, не сырые числа."""
    # cfg доступен (advantage/плейсменты/ставки) — в этом скетче в правила ещё не заводим.
    return {
        "saturation": saturation_signal(snaps),
        "attribution": attribution_check(a),
        "dissonance": dissonance_signal(a, ctr_median, cvr_median),
        "offer": offer_signal(a),
        "campaign_pacing": campaign_pacing(c),
    }
