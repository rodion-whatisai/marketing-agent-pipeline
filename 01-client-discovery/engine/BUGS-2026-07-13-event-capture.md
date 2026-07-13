# Найденные проблемы перехвата событий — 2026-07-13

Источник: расследование расхождения «отчёт artbouquet.shop говорит "Meta шлёт только
PageView", а Meta Pixel Helper вручную показывает AddToCart и InitiateCheckout».
Диагностика на 7 прогонах / 6 доменах: локальные данные в
`scans/_diag_meta_post_2026-07-13/` (README + сырые POST-тела), fixtures в
`scans/_fixtures/post_bodies/` (59 файлов). Обе папки в .gitignore — этот файл
фиксирует выводы в git.

## Проблема 1 — POST-слепота парсера событий (главная)

Оба сетевых слушателя — `scanners/base_scanner.py` `make_listeners.on_request`
(~L359) и `clicker.py` `make_pixel_listener` (~L78) — берут имя события через
`get_event_from_url` (~L270): **только из query-строки URL**.

Факты (матрица 2026-07-13):
- Meta шлёт содержательные события **POST'ом multipart/form-data** (блок `name="ev"`),
  когда payload не влезает в URL (~2KB): ViewContent на загрузке продуктовой —
  на ВСЕХ 4 доменах с Meta e-com событиями (artbouquet, allbirds, gymshark,
  pipsnacks) лежал в POST-теле, ни разу в query. AddToCart (artbouquet) — тоже POST.
- TikTok шлёт **всё** в JSON-телах (`"event": "..."`) — сканер не видел ни одного
  TikTok-события никогда.
- GA4 кладёт `en=` в query даже у POST — поэтому GA всегда парсился нормально.
- headless == headed (artbouquet A/B, идентичные события) — прежние выводы
  «headless глушит Meta» для этого класса сайтов были слепотой парсера,
  не поведением сайта.

Следствие: события деградировали в noise `"fired"` → системные false GAP.
Ошибка всегда в сторону занижения чужого трекинга.

Затронутые вердикты прошлых сканов:
- Meta false-negative: artbouquet.shop (2026-07-12), allbirds.com (шейкдаун 07-08).
- TikTok «конверсий нет» не доказан: artbouquet, allbirds, gymshark, bobbies.
- НЕ затронуты: сайты без Meta/TikTok или где всё летело GET'ом (photographersmontreal,
  kogerstaete, thebodyshop, tinytronics, studioaplus и др.).

Фикс (план): `get_event_from_request(request, platform)` — аддитивно, тело читается
только когда URL дал "fired"; multipart Meta + JSON TikTok + form-encoded generic;
тесты на 59 fixtures.

## Проблема 2 — отчёт игнорирует клик-события

`report.py` `analyze_platform_data` (~L97) строит «Покрытие по платформам»,
«ОТСУТСТВУЮЩИЕ СТАНДАРТНЫЕ СОБЫТИЯ» и фразу «Алгоритм Meta работает вслепую»
**только из `pixel_events`** (пассивная загрузка). События кликера живут в
`click_result` и вливаются лишь в `conversion_events_found` (step2_scan.py ~L209).

Следствие: даже пойманное по клику событие в платформенный анализ не попадает.
Подтверждённые случаи: pipsnacks (кликер поймал Meta:AddToCart — отчёт: «конверсий
НЕТ»), nissan.ie (пойманы Meta:Lead, Meta:Purchase — то же).

Фикс (план): клик-события в `pixel_events` с `source: "click"`; отчёт различает
«на загрузке» / «по клику»; статусная логика OK/GAP не меняется.

## Проблема 3 — «не проверялось» маскируется под «отсутствует»

Отчёт (`report.py` ~L582) объявляет InitiateCheckout/Purchase «отсутствующими»,
хотя сканер никогда не посещает checkout и не совершает покупку — эти события
недостижимы by design. Формулировка должна быть «не проверяется автосканом»,
отдельно от реальных пробелов.

## Проблема 4 — кликер не умеет Shopify-листинги

На /collections/* все «Add to cart»/«Choose options»/«Sold out» падают по таймауту
5s (artbouquet, обе коллекции). `shopify_scanner` не заполняет `cta_buttons` →
кликер использует generic `discover_buttons`, который не знает `[name="add"]`,
quick-view, cart drawer. Вариант товара не выбирается. → Отложено (отдельная сессия).

## Проблема 5 — journey cart→checkout отсутствует

Кликер намеренно давит навигацию (Ctrl+клик + reload-recovery), /cart посещается
только транзитом, checkout — никогда. Диагностика показала дополнительно: на
artbouquet корзина требует выбрать дату доставки до чекаута → journey-фича сложнее,
чем «кликнуть Checkout». → Отложено (фича из CLAUDE.md).

## Проблема 6 — Pinterest-правила нет в PIXEL_RULES

Pinterest-события реально летят (viewcontent на gymshark, PageVisit на pipsnacks,
частично тоже в POST-телах), но платформы нет в `PIXEL_RULES` → сканер её не
регистрирует вообще. → Бэклог.

## Побочная разгадка — «дубль GA4» на artbouquet

`G-5QB5QE16ZC` — из темы/GTM (top-level), `G-R6PH1ESB46` — изнутри Shopify custom
pixel `web-pixel-91750677` (sandbox). Не обязательно мисконфиг/двойной счёт одной
property — две разные property из двух мест. Отчёт мог бы аннотировать источник
каждого ID. → Бэклог.

## Статус

- Шаг 0 (валидационная матрица, n>1) — сделан 2026-07-13.
- Шаг 1 (POST-парсер) + Шаг 2 (отчёт) — план одобрен, в работе.
- Проблемы 4, 5, 6 и GA4-аннотация — отложены, отдельные сессии.
- После фиксов: пересканить allbirds/gymshark/bobbies/pipsnacks перед повторным
  использованием их вердиктов.
