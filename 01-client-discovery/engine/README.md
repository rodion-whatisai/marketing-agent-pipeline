# ▶ Как запустить локально (Quickstart)

Рабочий код стадии 01 (сканер трекинг-аудита). Ниже — всё, что нужно, чтобы склонировать и
прогнать у себя. Тестировалось на Windows; команды кросс-платформенные (macOS / Linux тоже).

> **TL;DR** (из корня репозитория):
> ```bash
> pip install -r requirements.txt        # зависимости
> playwright install chromium            # браузер для скана (обязательно!)
> cd 01-client-discovery/engine          # рабочая папка
> python step1_sitemap.py studioaplus.ca # прогон (пример: маленький сайт, 61 страница)
> ```
> На macOS используй `python3` вместо `python`, если `python` не найден.

---

## 1. Требования

- **Python 3.11+**
- Зависимости: `pip install -r requirements.txt` (playwright, requests, anthropic, colorama)
- **Chromium для Playwright:** `playwright install chromium` — без этого шаг 2 (скан) не запустится.
- (опц.) ключ Claude API — см. ниже. **Без ключа тоже работает**, просто грубее.

`requirements.txt` лежит в **корне репозитория**, не в этой папке.

## 2. ⚠️ API-ключ Claude — и что будет без него

Классификатор страниц работает в три слоя: `patterns.json` (выученные пути) → regex (общие
структурные правила) → **Claude Haiku** (всё, что первые два не распознали, батчами по 50).
Третий слой требует переменную окружения `ANTHROPIC_API_KEY`.

**Если ключ ЗАДАН** — полная классификация: нераспознанные правилами URL уходят в Claude и
получают точный тип.

**Если ключа НЕТ** (свежий клон без настройки) — тул **не падает**. Шаг Claude **молча
пропускается**: каждый URL, который `patterns.json`/regex не узнали, помечается типом
`general` (приоритет 5) и **в аудит (`to_scan`) не попадает**. На практике это значит: без
ключа сканируются только страницы, пойманные правилами (контакты, цены, чекаут и т.п.), а
всё нестандартное игнорируется. Результат корректный, но беднее. В логе будет
`ANTHROPIC_API_KEY не задан — N URL → general`.

> `.env` **не подхватывается автоматически** — задавай настоящую переменную окружения:

- **macOS / Linux (bash/zsh):**
  ```bash
  export ANTHROPIC_API_KEY=sk-ant-...
  ```
- **PyCharm:** Run → Edit Configurations → поле **Environment variables** →
  `ANTHROPIC_API_KEY=sk-ant-...`
- **Windows PowerShell:** `$env:ANTHROPIC_API_KEY = "sk-ant-..."`

## 3. Запуск пайплайна (по шагам)

Из папки `01-client-discovery/engine`:

```bash
# Шаг 1 — карта сайта, платформа, соцсети, Facebook Ads, классификация страниц
python step1_sitemap.py <домен>

# Шаг 2 — браузерный скан отобранных страниц (пиксели, события, CTA)
python step2_scan.py scans/<домен>/<домен>_step1.json

# Шаг 3 — текстовый отчёт + HTML
python report.py scans/<домен>/<домен>_step2.json

# Шаг 4 — склеить логи всех шагов в один файл
python merge_logs.py <домен>
```

Результаты — в `scans/<домен>/`: `_step1.json`, `_step2.json`, `_report.html`, `_audit_log.txt`.

**Полный прогон одной строкой** (пример на studioaplus.ca):
```bash
python step1_sitemap.py studioaplus.ca && python step2_scan.py scans/studioaplus.ca/studioaplus.ca_step1.json && python report.py scans/studioaplus.ca/studioaplus.ca_step2.json && python merge_logs.py studioaplus.ca
```

> Маленький сайт (≤65 страниц) проходит быстро и без вопросов. Большой (тысячи URL,
> напр. nissan.ie) — шаг 1 при заданном ключе долго гоняет классификацию через Claude;
> без ключа — быстро (Claude-слой скипается).

## 4. Логи

По умолчанию виден **весь поток** (уровень DEBUG): каждый шаг, ветка, решение — цветом по
уровням (INFO/OK/WARN/ERROR/DEBUG). Файл-лог (`scans/<домен>/*_log.txt`) — без цвета, с
тегами уровня (грепается по `[ERROR]` и т.п.).

Приглушить (только важное) — флаг `--quiet` или переменная `LOG_LEVEL`:
```bash
python step1_sitemap.py <домен> --quiet        # только INFO и выше
LOG_LEVEL=WARN python step1_sitemap.py <домен>  # только WARN/ERROR
```

## 5. Поведение без сети / за WAF

Если сайт за Cloudflare/WAF и не достучаться — тул честно помечает
`homepage_fetch_method = blocked_by_waf` и продолжает (не выдумывает данные). Стелс/прокси/
капча-солверы **не используются** by design.
