"""
03 · Execution — типы данных (КОНЦЕПТУАЛЬНЫЙ СКЕЛЕТ).

НЕ live: формы по докам Meta Marketing API, данные приходят из заглушек (meta_api.py).
Это не рабочий движок уровня 01/engine — это скетч структуры: какие входы, какие сигналы,
какое решение. Спроектировано, к реальному аккаунту не подключено.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class AdSet:
    """Снимок ad set: Meta-метрики + backend-данные (вход — из рекламного кабинета + бэкенда)."""
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
    attribution_delay_days: int   # типичный лаг клик→покупка
    # — backend (бэкенд магазина, не Meta) —
    backend_purchases: int
    new_paying_customers: int
    ltv: float
    refunds: int
    anchor_in_stock: bool         # якорный SKU в наличии


@dataclass
class ReachSnapshot:
    """Ежедневный снэпшот охвата — снимаем сами, каждый день."""
    day: int
    cumulative_reach: int   # Meta отдаёт дедуплицированный охват за период
    frequency: float


@dataclass
class AdSetConfig:
    """Настройки ad set — чтобы понимать, чем вообще управляем."""
    advantage_targeting: bool
    expand_reach: bool                 # галка «увеличивать охват»
    placements: list[str]
    expand_excluded_placements: bool   # «исключённые плейсменты ±5%»
    bid_rules: list[str]               # напр. ["+15% bid на women 25-34"]


@dataclass
class CampaignState:
    """Состояние кампании против плана (план — из стадии 02)."""
    id: str
    kpi_cpa_target: float
    planned_spend: float
    planned_result: int
    actual_spend: float
    actual_result: int
    days_elapsed: int
    days_total: int


@dataclass
class Signal:
    """Переваренный сигнал: Python уже интерпретировал — агент ест готовое, не сырьё."""
    name: str
    value: object
    verdict: str          # короткий человекочитаемый вывод


@dataclass
class Decision:
    """ОДНО конкретное действие."""
    adset_id: str
    action: str           # scale | hold | pause_candidate | send_to_human | do_nothing | reconcile_plan_vs_fact
    rationale: str


@dataclass
class AuditRecord:
    """JSON-след решения (форма из ТЗ ревьюера)."""
    adset_id: str
    signals: list[dict]
    rule_fired: str
    confidence: float
    blocked_by: list[str]
    required_approval: bool
    budget_before: float
    budget_after: float
    rollback_state: dict
