# Shakedown 2026-07-08 — 5 сайтов после 10 фиксов сессии

## ⚡ Для нового чата: старт отсюда

Контекст: 2026-07-07/08 сессия дебага закрыла 10 багов (коммиты `939348e..3c6b088`, все в master+GitHub).
Потом этот shakedown-прогон 5 свежих сайтов + независимая сверка нашли **12 новых багов** (список ниже,
секции A-D, с file:line и evidence). Rodion провалидировал и сказал херачить фиксы.

Рекомендуемый порядок (по вреду):
1. **A1** — Shopify substring false positives (фейковый «Snapchat ✅» в каждом Shopify-отчёте) — shopify_scanner.py:246-263, 356-359. Фикс: матчить не голые подстроки, а маркеры вызовов (`snaptr(`, `ttq.load`, app_id), и не синтезировать PageView без network-подтверждения.
2. **A2** — Segment из `analytics\.js` — gtm_analyzer.py:273. Убрать generic-паттерн.
3. **A3** — Cal.com из `cal.com/` подстроки — base_scanner.py:236 (EXTERNAL_SERVICES). Требовать границу (`//cal.com`, `"cal.com`, `.cal.com` нет — думать).
4. **A4** — Meta из пустого `"metaPixelId":""` — shopify_scanner.py:352-355 regex. Требовать непустой ID.
5. **B5/B6** — Pinterest в PIXEL_RULES+gtm_analyzer; Snapchat в gtm_analyzer (в PIXEL_RULES уже есть).
6. **C8** — redirect-чек финального URL после goto (fritz-kola кейс).
7. **C9** — 404/soft-404 чек перед аудитом страницы.
8. Остальное (C10, D11, D12) — по остаточному принципу.

Правила проекта: перед коммитом `git tag -f snapshot-before-merge`; замена существующего кода — сначала diff, апрув, потом правка; после фикса — верификация на живом кейсе из этого прогона + регрессия (tinytronics = быстрый эталон, 2 страницы); test-result комментарий в код; commit+push вместе.
Условие прогонов: ANTHROPIC_API_KEY в shell не было (classifier regex-only).
Журнал сверки: workflow `wf_0da994b6-3f0` (subagents/workflows). Артефакты: `scans/<домен>/`.

Прогон: step1 `--all` → step2 `--max-pages 4` → report → merge. Без ANTHROPIC_API_KEY (классификатор regex-only).
Сверка: 5 независимых агентов (curl-разведка сайта → сравнение с артефактами; журнал workflow `wf_0da994b6-3f0`).
Артефакты: `scans/<домен>/` (step1/step2 json, report.html, run_shakedown.log).

## Сводка

| Сайт | Платформа | Sitemap | Consent | Пиксели пойманы | Вердикт сверки |
|---|---|---|---|---|---|
| allbirds.com | Shopify high/60 ✅ | sitemap.xml, 2136 ✅ | CMP есть, баннера нет (US) ✅ | Meta+GA4+3×Ads+TikTok (все с ID) ✅ | 10/12 MATCH |
| bombas.com | Shopify high/27 ✅ | sitemap.xml, 1567 ✅ | OneTrust принят ✅ / гео-модал пропущен ❌ | Meta+GA4+2×Ads(дубль!)+Bing ✅ | 9/12 MATCH |
| fritz-kola.de | Shopify high/51 ✅ | fallback (домен-редирект) ✅ | принят (Pandectes) ✅ | GA4+Ads+Meta ✅ | 8/12 MATCH |
| gymshark.com | Shopify high/48 ✅ | sitemap.xml, 9507 ✅ | OneTrust принят ✅ | Meta×2(дубль!)+GA4+2×Ads+TikTok+Bing ✅ | 10/12 MATCH |
| pipsnacks.com | Shopify high/54 ✅ | sitemap.xml, 155 ✅ | CMP нет (US) ✅ | Meta+GA4+Ads+TikTok+Pinterest; 2 стр. ✅ OK (AddToCart по клику!) | 11/12 MATCH |

Сегодняшние фиксы подтверждены на новых сайтах: TikTok-правила (3 сайта, с ID), consent-first (2 OneTrust-сайта приняты), sitemap/robots, nav-CTA-фильтр (меню нигде не протекло в CTA), SSL — крашей нет.

## Новые баги (подтверждены evidence, НЕ чинились — ждут решения Rodion'а)

**A. Ложные платформы в клиентском отчёте (приоритет: доверие)**
1. **«Snapchat ✅» на любом Shopify** — `detect_shopify_pixel_platforms` (shopify_scanner.py:246-263) матчит голую подстроку `snapchat` в JS web-pixels-manager: она есть в UA-регэкспе `/(chromium|instagram|snapchat)/i` рантайма Shopify НА КАЖДОМ магазине. Плюс синтезированный PageView (356-359). То же слово `bing` → `/bingbot/i`. Кейс: allbirds (0 Snap-запросов из 1200, а в отчёте ✅ + рекомендация «бюджет Bing без attribution»).
2. **«Segment» из gtm_analyzer на 3 из 5 сайтов** — сигнатура `analytics\.js` (gtm_analyzer.py:273) матчит рантайм САМОГО gtm.js. Убрать generic-паттерн, оставить segment.com/cdn.segment.
3. **Cal.com false positive** (pipsnacks) — EXTERNAL_SERVICES `cal.com/` подстрокой матчит `tetralogiCAL.COM/` внутри бандла AccessiBe → фейковая «форма бронирования (Cal.com)» в unverified_pages.
4. **Meta из пустого pixelId** (jobs.fritz-kola.de) — HTML-regex матчит `"metaPixelId":""` (пустой!) и фабрикует Meta:PageView запись без source-флага.

**B. Слепые зоны**
5. **Pinterest не поддержан нигде** — на gymshark ct.pinterest.com бьёт в НАШИ ЖЕ persisted network_requests (5 хитов/стр., tid=2618098611272), в контейнере pintrk — ни PIXEL_RULES, ни gtm_analyzer его не знают. Criteo (allbirds контейнер) — тоже.
6. **gtm_analyzer не знает Snapchat** — в контейнере gymshark живые snaptr ADD_CART/PURCHASE теги, не отрепорчены (PIXEL_RULES добавили сегодня, gtm_analyzer — забыли).
7. **GTM ❌ при живом gtm.js** (pipsnacks) — контейнер инжектится runtime-скриптом из Shopify web pixel; find_tag_ids смотрит только DOM. gtm.js виден в наших же network_requests.

**C. Целостность прогона**
8. **Redirect-слепота** (fritz-kola.de) — все пути 301-ят на fritz-kola.com/ (путь отбрасывается); сканер трижды просканировал ОДНУ главную под видом cart/contact/product и не заметил смены хоста. Нужен чек финального URL после goto.
9. **404 аудируется как обычная страница** (bombas /collections/200-Giving-Back-Page-Test) — нет проверки статуса/soft-404 перед аудитом; в выборку категории попали тестовые слаги (alphabetical-first representative).
10. **Гео-модал bombas пропущен** (Canada interstitial) — handle_popups не нашёл, а кликер потом кликал его кнопки.

**D. Кликер/CTA мелочи**
11. Карусель по-немецки: «Vorheriger/Nächster Slide» прошли фильтр (SKIP_TEXTS англоязычный). Locale-кнопки 'CA'/'US' кликаются (gymshark — уводит на другой сторфронт). Accessibility-виджет кликается и мутирует сессию (bombas). h2-заголовки плиток как CTA (allbirds). Кликер 0/7 успешных кликов на allbirds (DOM переживает data-tnc-btn?).
12. Косметика: `--max-pages` пишется в step2.json как «sitemap_deduped» (это truncation); G-XXXXXXXXXX placeholder как реальный GA4 ID; «CMP blocking: true» при принятом consent; GT-ID подписывается как GTM-контейнер.

## Особенности сайтов (НЕ баги — для валидации Rodion'ом)
- allbirds: OneTrust в коде, но баннера в US-потоке нет — пиксели бьют сразу (gcs=G111). 7 AW-ID в контейнере, 3 стреляют.
- bombas: headless Next.js на Shopify — «Add to cart» в статике нет вообще; NO CTA на 4/4 страницах правдоподобен для этой выборки, но выборка попала в тестовые/404 слаги.
- fritz-kola.de: домен-редирект на .com; реальный магазин fritz-kola.com. Наш скан фактически аудировал .com-главную.
- gymshark: двойной редирект apex→checkout→www(headless); два Meta-пикселя реально дублируют (наш дубль-алерт — правда!).
- pipsnacks: трекеры грузятся отложенно (на homepage при первом заходе — тишина, дальше всё бьёт); 2 страницы честно ✅ OK — Meta:AddToCart пойман по клику.
