# 03 · engine — policy sketch (КОНЦЕПТУАЛЬНЫЙ СКЕЛЕТ, не live)

> **Это не рабочий движок уровня [01/engine](../../01-client-discovery/engine/).** Это
> концептуальный скелет execution-policy: видно входы, сигналы, решение и audit — но Meta
> API здесь **заглушки**, к реальному аккаунту не подключено. Спроектировано, не
> built-в-проде. Гоняется только на стабах.

## Поток

```
датасет из кабинета
   → Meta API (заглушки): insights · daily reach · config · campaign      meta_api.py
   → Python переваривает в сигналы (verdict'ы)                            signals.py
   → policy: ОДНО действие + JSON audit                                   policy.py
```

**Ключевой принцип:** LLM сырые данные читают плохо → Python отдаёт агенту уже **переваренные
сигналы**, не сырые числа. Где «судит» агент — помечено `agent_hook` (сейчас детерминированный
fallback). **Агент никогда не удаляет** — максимум `pause_candidate`.

## Файлы

- **`schema.py`** — типизированные входы/выходы: `AdSet` (Meta + backend поля), `ReachSnapshot`,
  `AdSetConfig`, `CampaignState`, `Decision`, `AuditRecord`. *(закрывает критику «no typed schemas»)*
- **`meta_api.py`** — **заглушки** с реальными эндпоинтами Meta Marketing API в докстрингах
  (видно, откуда и что тянули бы).
- **`signals.py`** — чистые детерминированные сигналы: **инкрементальный reach (не cumulative!)**
  + детект выгорания, «проверка невиновности» (атрибуция · сайт · оффер · сток), пейсинг кампании.
- **`policy.py`** — решение из пяти + reconcile: `scale / hold / pause_candidate / send_to_human /
  do_nothing / reconcile_plan_vs_fact` + JSON audit. `python policy.py` — демо на стабах.

## Запуск демо (на заглушках)

```
cd 03-execution/engine
python policy.py
```

На примере ad set «D» (по reach выгорел, но backend-конверсии выше Meta + диссонанс CTR/CVR)
policy **не рубит вслепую** — отдаёт `send_to_human`, потому что «проверка невиновности»
(атрибуция + сайт/оффер) блокирует kill. Это и есть защита от premature kill.

## Их 9 критериев → где в коде

| Критерий ревьюера | Файл / функция |
|---|---|
| delayed attribution | `signals.attribution_check` |
| premature-kill defense | `signals.innocence_check` |
| bad creative vs site vs offer vs stock | `dissonance_signal` · `offer_signal` · `anchor_in_stock` |
| budget conservation | `policy.build_audit` (+20% cap, иначе не трогаем) |
| campaign-level delivery | `signals.campaign_pacing` |
| decision logging | `AuditRecord` (JSON) |
| human gate | `decide` → `send_to_human` + `required_approval` |
| где агент судит vs код | `agent_hook_explain` (тонко) vs всё остальное (код) |
| testing on mock history | демо на стабах + снэпшоты reach |
