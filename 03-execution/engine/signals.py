"""
03 · Execution — формирование сигналов (Python переваривает, агент ест готовое).

Чистые детерминированные функции: сырьё → короткие сигналы (verdict'ы). Сигналы с малыми
данными гасятся (confidence) — не действуем на шуме.
"""
from schema import AdSet, ReachSnapshot, Signal

MIN_PURCH_FOR_DIAGNOSIS = 15   # ниже — данных мало, диагностику не запускаем


def median(xs: list) -> float:
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def incremental_reach(snaps: list) -> list:
    """ДОПОЛНИТЕЛЬНЫЙ охват день-к-дню (НЕ cumulative): сколько НОВЫХ людей дал Facebook."""
    out, prev = [], 0
    for s in snaps:
        out.append(s.cumulative_reach - prev)
        prev = s.cumulative_reach
    return out


def saturation_signal(snaps: list) -> Signal:
    inc = incremental_reach(snaps)
    last3 = inc[-3:] if len(inc) >= 3 else inc
    ratio = (sum(last3) / len(last3)) / inc[0] if inc and inc[0] else 0.0
    saturated = ratio < 0.05 and snaps[-1].frequency >= snaps[0].frequency
    return Signal("saturation", "выгорел" if saturated else "растёт",
                  f"новый охват {ratio:.0%} от старта")


def attribution_signal(a: AdSet) -> Signal:
    immature = a.attribution_delay_days >= 5 and a.purchases < a.backend_purchases
    return Signal("attribution", "immature" if immature else "mature",
                  "не дозрело: backend выше Meta" if immature else "окно закрыто")


def dissonance_signal(a: AdSet, ctr_med: float, cvr_med: float) -> Signal:
    """Направление: site (CTR↑/CVR↓) · creative (CTR↓/CVR↑) · ok · insufficient (мало данных)."""
    if a.purchases < MIN_PURCH_FOR_DIAGNOSIS:
        return Signal("dissonance", "insufficient", "данных мало — диагностику не запускаем")
    high_ctr, high_cvr = a.ctr >= ctr_med, a.cvr >= cvr_med
    if high_ctr and not high_cvr:
        return Signal("dissonance", "site", "CTR↑ / CVR↓ → проблема сайта")
    if not high_ctr and high_cvr:
        return Signal("dissonance", "creative", "CTR↓ / CVR↑ → проблема креатива")
    return Signal("dissonance", "ok", "клик↔конверсия согласованы")


def utility_signal(a: AdSet, total_spend: float, total_purch: int) -> Signal:
    """%продаж ÷ %бюджета. >1 тянет выше веса, <1 балласт (он же «низ таблицы»)."""
    share_spend = a.spend / total_spend if total_spend else 0
    share_purch = a.purchases / total_purch if total_purch else 0
    u = (share_purch / share_spend) if share_spend else 0.0
    return Signal("utility", round(u, 2), f"{u:.2f} (низ таблицы)" if u < 1 else f"{u:.2f}")


def budget_signal(a: AdSet) -> Signal:
    """Выбирает ли дневной бюджет за последние 5 дней (≥80% от cap в ≥4 из 5 дней)."""
    hit = sum(1 for s in a.spend_5d if s >= 0.8 * a.budget_day)
    realized = hit >= 4
    return Signal("budget", "выбирает" if realized else "не выбирает", f"{hit}/5 дней у cap")


def offer_signal(a: AdSet) -> Signal:
    rate = a.refunds / max(a.backend_purchases, 1)
    bad = rate > 0.15 or a.ltv < a.cpa
    return Signal("offer", "слабый" if bad else "ок",
                  f"refunds {rate:.0%}, LTV ${a.ltv:.0f}")


def site_scan_stub(a: AdSet) -> dict:
    """[ЗАГЛУШКА стадии 01] прогон сайт-сканером: OOS / проблема конверсии на сайте."""
    return {"oos": not a.anchor_in_stock, "site_problem": True}


def digest(a: AdSet, snaps: list, ctr_med: float, cvr_med: float,
           total_spend: float, total_purch: int) -> dict:
    """Сырьё → словарь переваренных сигналов. Это кормим агенту, не сырые числа."""
    return {
        "utility": utility_signal(a, total_spend, total_purch),
        "budget": budget_signal(a),
        "dissonance": dissonance_signal(a, ctr_med, cvr_med),
        "attribution": attribution_signal(a),
        "saturation": saturation_signal(snaps),
        "offer": offer_signal(a),
    }
