"""
03 · Execution — типы данных (КОНЦЕПТУАЛЬНЫЙ СКЕЛЕТ).

Формы по докам Meta Marketing API, данные приходят из заглушек (meta_api.py).
Скетч структуры: входы, сигналы, решение. Спроектировано, к реальному аккаунту не подключено.
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class AdSet:
    """Снимок ad set: Meta-метрики + backend-данные (вход — из кабинета + бэкенда)."""
    id: str
    campaign_id: str
    name: str
    # — Meta insights —
    spend: float
    clicks: int
    purchases: int
    cpa: float
    roas: float
    ctr: float            # %
    cvr: float            # %
    learning_status: str  # "learning" | "done"
    last_edit_days: int
    budget_day: float
    attribution_delay_days: int
    spend_5d: list = field(default_factory=list)   # спенд по дням за последние 5 дней
    # — backend (бэкенд магазина, не Meta) —
    backend_purchases: int = 0
    new_paying_customers: int = 0
    ltv: float = 0.0
    refunds: int = 0
    anchor_in_stock: bool = True


@dataclass
class ReachSnapshot:
    """Ежедневный снэпшот охвата — снимаем сами."""
    day: int
    cumulative_reach: int
    frequency: float


@dataclass
class AdSetConfig:
    """ЧЕМ управляем — читаем из Meta (в правила заводим пока частично)."""
    advantage_plus: bool          # Advantage+ audience / campaign
    budget_optimization: str      # "CBO" | "ABO"
    bid_strategy: str             # "lowest_cost" | "cost_cap" | "roas_goal" | "bid_cap"
    cost_cap: float               # 0 если не задан
    roas_goal: float              # 0 если не задан
    optimization_event: str       # "purchase" | "add_to_cart" | ...
    attribution_setting: str      # "7d_click_1d_view" | "1d_click" | ...
    placements: list
    expand_reach: bool


@dataclass
class Rule:
    """Правило в конструкторе: кандидат → тест → промоут в постоянную Python-иерархию."""
    id: str
    name: str
    status: str        # "candidate" | "testing" | "promoted"
    applies_to: str    # "campaign" | "ad_set" | "ad"
    success_case: str  # как меряем, что правило сработало
    validated_by: str  # "python" | "human" | "—"


@dataclass
class CampaignState:
    id: str
    kpi_cpa_target: float
    planned_spend: float
    planned_result: int
    actual_spend: float
    actual_result: int
    days_elapsed: int
    days_total: int
    budget_mode: str = "ABO"   # "ABO" (бюджет на ad set) | "CBO"/Advantage+ (на кампании)


@dataclass
class Signal:
    """Переваренный сигнал: Python интерпретировал — агент ест готовое, не сырьё."""
    name: str
    value: object
    verdict: str


@dataclass
class Decision:
    """ОДНО конкретное действие."""
    adset_id: str
    action: str           # scale | hold | pause_candidate | send_to_human | do_nothing
    rationale: str


@dataclass
class AuditRecord:
    adset_id: str
    signals: list
    rule_fired: str
    confidence: float
    blocked_by: list
    required_approval: bool
    budget_before: float
    budget_after: float
    rollback_state: dict
