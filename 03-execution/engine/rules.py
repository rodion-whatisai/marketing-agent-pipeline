"""
03 · Execution — rule-constructor (КОНЦЕПТУАЛЬНЫЙ).

Движок — не «решённый оптимизатор», а КОНСТРУКТОР правил:
правило-кандидат → ТЕСТ → если прошло проверку (Python: «результаты стали лучше», или
человек: «стали лучше») → промоут в ПОСТОЯННУЮ Python-иерархию (policy.decide). Пул
«на тесте» обновляется.

Оптимальная ПОСЛЕДОВАТЕЛЬНОСТЬ рассмотрения всех аргументов — вопрос ИССЛЕДОВАНИЯ.
Сейчас: сбор максимума данных + простые модели (вкл / выкл / изменить, при прочих равных).
"""
import meta_api
from schema import Rule

RESERVED = [   # ОБЯЗАТЕЛЬНЫЕ гард-правила: всегда включены, не кандидаты, не отключаются и
               # не «промоутятся» — это инварианты движка, проверяются кодом всегда.
    Rule("cbo_no_adset_budget",
         "CBO / Advantage+ → бюджет двигаем на уровне КАМПАНИИ, не ad set", "reserved", "campaign",
         "не трогали ad-set-бюджет там, где им управляет CBO (Meta перераспределит сама)",
         "python"),
]

# Что ЧИТАЕМ на входе (данные есть — комбинируем пока частично). См. meta_api.fetch_adset_config.
INTAKE_FIELDS = [
    "Advantage+ (вкл/выкл)", "CBO / ABO", "bid strategy (lowest_cost / cost_cap / roas_goal)",
    "cost cap / ROAS goal", "optimization_event", "attribution_setting (окно)", "placements",
    "spend_5d · reach · frequency", "backend: purchases / LTV / refunds / stock",
]

PROMOTED = [   # сейчас в постоянной иерархии policy.decide
    Rule("innocence", "Проверка невиновности (атрибуция/сайт/сток)", "promoted", "ad_set",
         "не убили ad set с внешней причиной → не слили бюджет зря", "python"),
    Rule("learning_hold", "Learning hold", "promoted", "ad_set",
         "не сбросили learning преждевременной правкой", "python"),
    Rule("scale_if_realized", "Scale только если бюджет выбирается", "promoted", "ad_set",
         "не повышали бюджет там, где он и так не осваивается", "python"),
    Rule("creative_diagnosis", "Плохой CPA + CTR↓/CVR ок → креатив", "promoted", "ad/ad_set",
         "отделили проблему креатива от проблемы сайта", "human"),
    Rule("burnout_pause", "Выгорел по reach → pause_candidate", "promoted", "ad_set",
         "сняли спенд с исчерпанной аудитории", "python"),
]

CANDIDATES = [   # на тесте — заводятся в микс по мере подтверждения
    Rule("cbo_abo_budget", "CBO vs ABO: бюджет двигается по-разному", "testing", "campaign",
         "корректная аллокация при CBO (уровень кампании), не ad set", "—"),
    Rule("cost_cap_breach", "Cost cap систематически пробит → пересмотр", "candidate", "ad_set",
         "вовремя поймали нереалистичный cap", "—"),
    Rule("attribution_window_aware", "Решение с учётом окна (7d_click vs 1d)", "candidate", "ad_set",
         "не судили по неполному окну", "—"),
    Rule("incrementality_holdout", "Инкрементальность через holdout", "candidate", "campaign",
         "отличили реальный лифт от каннибализации", "—"),
]


def intake(adset_id: str = "adset_D"):
    """Читаем конфиг ad set — показываем, что данные на входе есть."""
    return meta_api.fetch_adset_config(adset_id)


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    cfg = meta_api.fetch_adset_config("adset_D")
    print("ЧИТАЕМ про ad set:", " · ".join(INTAKE_FIELDS))
    print(f"  пример конфига: Advantage+={cfg.advantage_plus} · {cfg.budget_optimization} · "
          f"bid={cfg.bid_strategy} · cost_cap=${cfg.cost_cap:.0f} · окно={cfg.attribution_setting}")
    print(f"\nReserved (обязательные инварианты, всегда вкл): {len(RESERVED)}")
    for r in RESERVED:
        print(f"  ⛔ {r.name} [{r.applies_to}] — {r.success_case}")
    print(f"\nПОСТОЯННЫЕ правила в иерархии (policy.decide): {len(PROMOTED)}")
    for r in PROMOTED:
        print(f"  ✓ {r.name} [{r.applies_to}] · валид: {r.validated_by}")
        print(f"      success: {r.success_case}")
    print(f"\nНА ТЕСТЕ (кандидаты — в микс по мере подтверждения): {len(CANDIDATES)}")
    for r in CANDIDATES:
        print(f"  · {r.name} [{r.status}, {r.applies_to}]")
    print("\nОптимальная ПОСЛЕДОВАТЕЛЬНОСТЬ всех аргументов — вопрос исследования.")
    print("Сейчас: максимум данных + простые модели (вкл/выкл/изменить, при прочих равных).")
