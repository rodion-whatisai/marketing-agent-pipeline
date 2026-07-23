# golden/ — золотой корпус испытательного стенда

Что это и зачем — в [TESTBED-PLAN.md](../TESTBED-PLAN.md). Коротко:

- **Эталон = правда, а не текущий вывод сканера.** В `expected_<domain>.json` записано,
  что сканер ОБЯЗАН видеть на сайте (проверено человеком). Если сканер сейчас этого
  не видит — это известный провал (known fail), он должен гаснуть по мере фиксов.
- **step1 заморожен**: `golden/<domain>/step1.json` — копия прогона; step2 в стенде
  всегда бежит по одному и тому же списку URL, чтобы счёт не шумел от живого sitemap.
  Обновление — только сознательное (`eval_run.py --refresh-step1`).
- **Усечение fast-доменов делается в самом замороженном step1** (правка `to_scan`,
  пометка `_testbed_note` в файле), а НЕ флагом `--max-pages` — тот искажает счётчики
  (баг D12). tinytronics усечён до 2 страниц: `/` (кейс OK) + `/en/comment-or-suggestion`
  (кейс GAP); полный список остаётся в `classified`.
- Состав корпуса и причины выбора каждого сайта — `corpus.json`.
- История счёта по прогонам — `history.csv` (коммитится, это кривая доверия).

## Правило гейта и два сорта полей (ревизия 2026-07-13)

**Любая правда записывается сюда ТОЛЬКО после явного «да» Rodion'а** — с уликой
свидетеля в вопросе. Кто что подтверждает:

| Сорт | Поля | Кто подтверждает |
|---|---|---|
| **«Правда о сайте»** — существует независимо от нашего кода | `platforms_detected`, `platforms_forbidden`, `gtm_platforms`, `conversion_events_min`, `external_services`, `has_cta`, redirect/HTTP- и consent-факты | **только Rodion, явным «да»**; улики — witness-файлы рядом; запись — в `verified_via` |
| **«Контракт сканера»** — договорённость, как ярлычить правду | `page_type` (конвенция классификации), `status` (ярлык при данной правде), `counters`, `missing_events` | скан-derived — допустимо, честно подписано «контракт» |

Файлы свидетеля: `golden/<domain>/witness_<date>.json` (обход страниц: финальный
URL+статус, пиксель-запросы с методами и POST-телами, сырые кликабельные тексты,
сторонние хосты) и `witness_journey_<date>.json` (e-com путь product→ATC→cart→
checkout). Сырьё (скриншоты, полные тела) — `scans/_witness_<date>/`, gitignored.

Машинная проверка: `python witness_check.py <domain>` — «эталон не противоречит
сырому трафику»: ✅ подтверждено / ⚠ не засвидетельствовано (на гейт) /
❌ противоречие (exit 1).

## Формат expected_<domain>.json (schema_version 2)

Проверяются ТОЛЬКО стабильные поля. Отсутствующее в эталоне поле = не проверяется.

```json
{
  "schema_version": 2,
  "domain": "fritz-kola.de",
  "verified_by": "rodion",          // "draft" = черновик, правды в нём НЕТ
  "verified_date": "2026-07-15",
  "verified_against": "гейт-раунд в чате + ручная сверка",
  "verified_via": {                 // чем подтверждали (schema v2)
    "witness": "golden/fritz-kola.de/witness_2026-07-15.json",
    "rodion_gate": "чат 2026-07-15: подтвердил платформы, запреты, кнопки",
    "rodion_manual": "Pixel Helper / Tag Assistant"
  },
  "scanner_commit": "f7e9a6b",
  "notes": "Домен 301-ит на fritz-kola.com, пути схлопываются на главную.",
  "site": {
    "platform": "shopify",
    "gtm_platforms": ["Google Analytics", "Google Ads", "Meta"],
    "counters": {"gaps": 0, "oks": 1, "no_ctas": 0, "no_tracking": 0, "unverified": 0}
  },
  "pages": {
    "/": {
      "status": "OK",
      "page_type": "homepage",
      "has_cta": true,
      "platforms_detected": ["Meta", "Google Analytics", "Google Ads"],
      "external_services": [],
      "missing_events": []
    }
  }
}
```

Правила:
- `status` — нормализованный (OK / GAP / NO_TRACKING / NO_CTA / UNVERIFIED,
  позже REDIRECTED / HTTP_ERROR), НЕ emoji-строка из step2.json.
- `platforms_detected` / `gtm_platforms` / `external_services` — сравнение
  «ожидаемое ⊆ фактическое»; лишнее фактическое разбирает FAIL/DRIFT-логика стенда.
- `status` / `has_cta` / `counters` — точное совпадение.
- НИКОГДА не пишем в эталон волатильное: тексты кнопок, порядок списков,
  сырые network-запросы, точные event-списки кликов.

## Как обновлять

- Новый эталон / перезаверка: `python make_expected.py <domain>` (интерактив, y/n
  по каждой странице) или `--draft` (черновик без апрува, verified_by="draft").
- Сайт реально изменился (DRIFT в стенде, подтверждён глазами):
  `python make_expected.py <domain> --update` + новая verified_date.
