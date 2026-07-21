# Доступ скрипта к CPM-таблице (делается один раз, ~5 минут)

Чтобы `spend_estimate.py` читал живые цифры из твоей таблицы, а не мою
ручную выписку, нужен «робот-читатель» — **сервисный аккаунт**. Это
технический гугл-аккаунт, которому ты даёшь доступ к таблице как обычному
человеку, только вместо почты у него длинный адрес вида
`что-то@проект.iam.gserviceaccount.com`.


## Шаги

**1. Создать проект**
Открой https://console.cloud.google.com → вверху выпадающий список
проектов → **New project** → имя `tnc-pitch-adviser` → Create.
(Проект — просто папка для настроек, платить ничего не надо.)

**2. Включить доступ к Таблицам**
В поиске сверху набери **Google Sheets API** → открой → кнопка **Enable**.

**3. Создать робота**
Слева меню → **APIs & Services → Credentials** → сверху
**+ Create credentials → Service account**.
Имя: `pitch-adviser-reader` → **Create and continue** → раздел с ролью
**пропусти** (Continue) → **Done**.

**4. Скачать ключ**
В списке Service Accounts кликни на созданный → вкладка **Keys** →
**Add key → Create new key → JSON → Create**. Файл скачается сам.

**5. Положить ключ на место**
Переименуй скачанный файл в `gsheets.json` и положи сюда:
```
C:\Users\user\SiteScannerv4\01-client-discovery\pitch-adviser\.secrets\gsheets.json
```
(папку `.secrets` создай, если её нет). В git этот файл не попадёт —
он в `.gitignore`.

**6. Дать роботу доступ к таблице**
Открой файл `gsheets.json` блокнотом, найди строку `"client_email"` —
скопируй адрес из неё. Открой
[CPM-таблицу](https://docs.google.com/spreadsheets/d/16k73ARvb1zHV-4vMTm5iulz0EsBdXJJo1Ruo-bQMuHM/edit)
→ **Share** → вставь этот адрес → права **Viewer** → Share.

## Проверка

```bash
cd 01-client-discovery/pitch-adviser
python spend_estimate.py client-a.example
```
В выводе должно быть: `бенчмарки: живая таблица (обновлена …)` и строка,
которую скрипт выбрал для расчёта.

## Если что-то пойдёт не так

Скрипт не упадёт: при любой проблеме с доступом он возьмёт последнюю
удачную выгрузку `benchmarks/cpm-cache.csv` и предупредит, какого она
числа. Просто скажи мне — разберёмся.
