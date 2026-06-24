"""
03 · Execution — Meta Marketing API (ЗАГЛУШКИ).

Функции возвращают мок; в докстрингах — реальные эндпоинты. Не подключено к аккаунту.
"""
from schema import AdSet, ReachSnapshot, AdSetConfig, CampaignState

ACCOUNT = "act_<AD_ACCOUNT_ID>"


def fetch_portfolio() -> list:
    """
    Real:  GET /{campaign_id}/adsets + /{adset_id}/insights (spend,clicks,actions,roas,ctr...)
           + backend-джойн (purchases/new_customers/ltv/refunds/stock) по order attribution.
    Здесь — 5 ad set'ов с разными историями (winner / kill / learning / атрибуция / усталый).
    """
    # STUB
    return [
        AdSet("adset_A", "camp_1", "A · winner", 4200, 8400, 210, 20, 3.0, 2.8, 2.5,
              "done", 12, 600, 2, [600, 600, 580, 600, 600], 210, 180, 60, 3, True),
        AdSet("adset_B", "camp_1", "B · bleeding", 3000, 1350, 33, 91, 0.66, 0.9, 2.4,
              "done", 18, 300, 2, [300, 300, 300, 290, 300], 33, 30, 60, 2, True),
        AdSet("adset_C", "camp_1", "C · learning", 280, 333, 6, 47, 1.28, 1.9, 1.8,
              "learning", 2, 80, 3, [40, 60, 80, 80, 20], 6, 6, 60, 0, True),
        AdSet("adset_D", "camp_1", "D · attribution_trap", 1500, 2538, 18, 83, 0.72, 2.2, 0.7,
              "done", 9, 250, 7, [200, 250, 250, 400, 400], 31, 24, 140, 1, True),
        AdSet("adset_E", "camp_1", "E · creative_fatigue", 5800, 2658, 145, 40, 1.5, 1.1, 5.5,
              "done", 25, 700, 2, [700, 700, 680, 700, 700], 145, 60, 60, 5, True),
    ]


def fetch_daily_reach(adset_id: str, days: int = 15) -> list:
    """
    Real:  GET /{adset_id}/insights?fields=reach,frequency&time_increment=1
    Кумулятив снапшотим сами, дополнительный reach = разница день-к-дню (signals.incremental_reach).
    Здесь — заглушка: D выходит на плато (выгорание), остальные растут.
    """
    # STUB
    if adset_id == "adset_D":
        base = [5000, 11000, 16000, 20000, 23000, 25000, 26500, 27500,
                28200, 28700, 29000, 29150, 29250, 29300, 29330]
    else:
        base = [4000, 9000, 14000, 19000, 24000, 29000, 34000, 39000,
                44000, 49000, 54000, 59000, 64000, 69000, 74000]
    return [ReachSnapshot(i + 1, base[i], 1.0 + i * 0.1) for i in range(min(days, len(base)))]


def fetch_adset_config(adset_id: str) -> AdSetConfig:
    """Real: GET /{adset_id}?fields=targeting,optimization_goal,bid_strategy,..."""
    # STUB
    return AdSetConfig(True, True, ["facebook_feed", "instagram_feed"], False, [])


def fetch_campaign(campaign_id: str) -> CampaignState:
    """Real: GET /{campaign_id}?fields=daily_budget,... + /insights. План — из стадии 02."""
    # STUB
    return CampaignState("camp_1", 30.0, 60000, 2000, 14780, 412, 8, 30)
