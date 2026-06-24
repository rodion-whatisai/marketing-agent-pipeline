# 03 · engine — policy sketch (КОНЦЕПТУАЛЬНЫЙ СКЕЛЕТ, не live)

> **Это не рабочий движок уровня [01/engine](../../01-client-discovery/engine/).** Скелет
> execution-policy: видно входы, сигналы, решение и audit — но Meta API здесь **заглушки**,
> к реальному аккаунту не подключено. Спроектировано, не built-в-проде. Гоняется на стабах.

## Поток

```
портфель ad set из кабинета
   → Meta API (заглушки): insights · daily reach · config · campaign      meta_api.py
   → Python переваривает в сигналы (verdict'ы)                            signals.py
   → policy: ОДНО действие + маршрут (python / human) + JSON audit        policy.py
```

**Ключевой принцип:** LLM сырые данные читают плохо → Python отдаёт агенту **переваренные
сигналы**, не сырые числа. **Агент никогда не удаляет** — максимум `pause_candidate`.

## Порядок гейтов (`policy.decide`)

1. **Невиновность.** Атрибуция не дозрела → `hold` (wait, авто). Сайт-диссонанс (CTR↑/CVR↓)
   или сток → **сначала прогон сканером 01** → если подтвердил → `send_to_human`.
2. **Learning.** `learning` → `hold` (не трогаем).
3. **Эффективность.** CPA ≤ target + utility ≥ 1 + бюджет выбирается → `scale` +20% (reserved-гард:
   при **CBO/Advantage+** ad-set scale подавляется → campaign-level). Плохой (CPA > target) →
   диагностика: CTR↓/CVR ок → креатив → `send_to_human`; выгорел по reach → `pause_candidate`.

Сигналы с малыми данными гасятся (confidence) — фреш-learner не уходит к человеку по шуму.

## Файлы

- **`schema.py`** — типы: `AdSet` (Meta + backend + `spend_5d`), `ReachSnapshot`, `Config`,
  `CampaignState`, `Decision`, `AuditRecord`. *(закрывает «no typed schemas»)*
- **`meta_api.py`** — заглушки; `fetch_portfolio()` = 5 ad set; реальные эндпоинты в докстрингах.
- **`signals.py`** — детерминированные сигналы: **инкрементальный reach (не cumulative)** +
  выгорание, **диссонанс с направлением** (site / creative, гасится при малых данных),
  utility-коэффициент, budget-utilization, attribution, offer, `site_scan_stub` (стадия 01).
- **`policy.py`** — `decide()` (3 гейта) → одно действие + маршрут + JSON audit; `run_portfolio()`.
- **`test_policy.py`** — pytest, пинит 5 исходов (чтобы багфиксы не регрессировали).

## Запуск

```
cd 03-execution/engine
python policy.py        # лог решений по 5 ad set
python -m pytest -q     # 8 проверок  (нужен: pip install pytest)
```

## Лог демо (5 ad set)

| ad set | действие | маршрут | почему |
|---|---|---|---|
| **A** winner | `scale` +20% | python-resolver | CPA $20 ≤ target, utility 1.79, бюджет выбирается |
| **B** bleeding | `send_to_human` | human | CTR↓ / CVR ок → креатив (новые / выключить low-CTR) |
| **C** learning | `hold` | python (auto) | learning — не трогаем |
| **D** attribution | `hold` | python (auto-wait) | атрибуция не дозрела → wait (не судим свежий спенд) |
| **E** fatigue | `send_to_human` | human | CTR↓ / CVR ок → креатив (усталость) |

## Их 9 критериев → где в коде

| Критерий ревьюера | Где |
|---|---|
| delayed attribution | `signals.attribution_signal` → gate 1 (wait) |
| premature-kill defense | gate 1 невиновность + гашение сигналов по данным |
| creative vs site vs offer vs stock | `dissonance_signal` (направление) · `offer_signal` · `site_scan_stub` |
| budget conservation | `budget_signal` + `+20%` cap в `build_audit` |
| campaign-level delivery | `CampaignState` target; кампанийный форкаст — в [../README](../README.md) |
| decision logging | `AuditRecord` (JSON), `route` |
| human gate | `decide` → `send_to_human` / `required_approval` |
| где агент судит vs код | всё детерминированно; агент-хук тонкий (формулировка «почему») |
| testing on mock history | `test_policy.py` + reach-снэпшоты |
