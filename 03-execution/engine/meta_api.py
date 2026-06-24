"""
03 · Execution — Meta Marketing API (ЗАГЛУШКИ).

Ни одной реальной сети: функции возвращают мок. В докстрингах — реальные эндпоинты
Meta Marketing API, чтобы было видно, ОТКУДА и ЧТО мы бы тянули. Не подключено к аккаунту.
"""
from schema import AdSet, ReachSnapshot, AdSetConfig, CampaignState

ACCOUNT = "act_<AD_ACCOUNT_ID>"   # сюда подставляется реальный ID аккаунта


def fetch_insights(adset_id: str, last_n_days: int = 5) -> AdSet:
    """
    Real:  GET /{adset_id}/insights
           ?fields=spend,clicks,actions,cost_per_action_type,purchase_roas,ctr,...
           &date_preset=last_{n}d
    + backend-поля джойнятся из бэкенда магазина по utm/order attribution.
    Здесь — заглушка (ad set «D»: ловушка атрибуции).
    """
    # STUB
    return AdSet(
        id=adset_id, campaign_id="camp_1", name="D · attribution_trap",
        spend=1500, clicks=2538, purchases=18, cpa=83.0, roas=0.72,
        ctr=2.2, cvr=0.7, learning_status="done", last_edit_days=9,
        budget_day=250, attribution_delay_days=7,
        backend_purchases=31, new_paying_customers=24, ltv=140.0, refunds=1,
        anchor_in_stock=True,
    )


def fetch_daily_reach(adset_id: str, days: int = 15) -> list[ReachSnapshot]:
    """
    Real:  GET /{adset_id}/insights?fields=reach,frequency&time_increment=1
    ВАЖНО: Meta отдаёт дедуплицированный reach. Кумулятив снапшотим САМИ ежедневно и
    считаем ДОПОЛНИТЕЛЬНЫЙ reach как разницу день-к-дню (см. signals.incremental_reach).
    Здесь — заглушка: охват растёт, потом выходит на плато (выгорание).
    """
    # STUB — кумулятивный охват выходит на плато к концу
    base = [5000, 11000, 16000, 20000, 23000, 25000, 26500, 27500,
            28200, 28700, 29000, 29150, 29250, 29300, 29330]
    return [ReachSnapshot(day=i + 1, cumulative_reach=base[i],
                          frequency=1.0 + i * 0.12) for i in range(min(days, len(base)))]


def fetch_adset_config(adset_id: str) -> AdSetConfig:
    """
    Real:  GET /{adset_id}?fields=targeting,optimization_goal,bid_strategy,
           targeting_optimization,...
    Отсюда: advantage+/нет, «увеличивать охват», плейсменты, «исключённые ±5%», правила ставки.
    Здесь — заглушка.
    """
    # STUB
    return AdSetConfig(
        advantage_targeting=True, expand_reach=True,
        placements=["facebook_feed", "instagram_feed", "instagram_stories"],
        expand_excluded_placements=False,
        bid_rules=[],
    )


def fetch_campaign(campaign_id: str) -> CampaignState:
    """
    Real:  GET /{campaign_id}?fields=daily_budget,spend_cap,... + /insights для факта.
    План (planned_spend / planned_result / kpi) — из стадии 02 (planning).
    Здесь — заглушка (кампания отстаёт, но держит KPI по стоимости... почти).
    """
    # STUB
    return CampaignState(
        id=campaign_id, kpi_cpa_target=30.0,
        planned_spend=60000, planned_result=2000,
        actual_spend=14780, actual_result=412,
        days_elapsed=8, days_total=30,
    )
