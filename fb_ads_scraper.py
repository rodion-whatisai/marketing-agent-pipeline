"""
TNC — Facebook Ads Library Scraper
==================================
Скрапит Facebook Ads Library через keyword search + пост-фильтр по page_id/name.

Стратегия (см. CLAUDE.md):
  - view_all_page_id-режим мёртв (2026-05-07 verified) — keyword search only.
  - Шум фильтруется в _extract_ads_from_json по snapshot.page_id (exact) и
    snapshot.page_name (fuzzy >= 0.85, поднят с 0.75 после false positive на
    Grays Locksmiths vs KeyMe Locksmiths).

Содержит:
  - build_ads_library_urls: URL builder
  - _walk_for_key / _normalize_ad_record / _extract_ads_from_json /
    _parse_ad_library_html: HTML/JSON парсинг
  - _download_ad_images: скачивание creative картинок
  - _build_ad_library_url / _scan_one_status: per-status scan (active/inactive/all)
  - get_ads_data: 3-проходный listing scan (orchestrator)

Используется: fb_page_id.py (orchestrator), fb_ads_listing.py, fb_page_finder.py.

Известная архитектурная проблема (см. CLAUDE.md):
  - _extract_ads_from_json ищет JSON только в <script type="application/json">.
    Новые версии FB UI могут hydrate'ить вне этого тега → structured_ads пустой,
    count работает только через regex fallback.
"""

import re
import json

from utils import scan_path, setup_console
setup_console()


# ─── Ads Library URLs ─────────────────────────────────────────────────────────

def build_ads_library_urls(display_name: str, countries: list = None, page_id: str = None) -> dict:
    """Строит ссылки на Ads Library — keyword search.

    page_id параметр принимается для back-compat (некоторые callers передают),
    но НЕ используется в URL: view_all_page_id-режим был протестирован 2026-05-07
    и подтверждён мёртвым для big brands даже в залогиненном браузере (KeyMe
    Locksmiths: page-mode возвращает "No ads match" хотя у них реально 11+
    активных ads, видимых в keyword search). Не возвращаемся к этой идее.

    Стратегия: keyword search → пост-фильтр по snapshot.page_id (exact) или
    snapshot.page_name (fuzzy match >= 0.85). См. _extract_ads_from_json.
    """
    keyword = display_name.strip().replace(" ", "%20")
    base = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=active&ad_type=all&country=ALL"
        f"&is_targeted_country=false&media_type=all"
        f"&q={keyword}"
        f"&search_type=keyword_unordered"
        f"&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )
    return {
        "ALL": {
            "active_only":   base,
            "all":           base.replace("active_status=active", "active_status=all"),
            "inactive_only": base.replace("active_status=active", "active_status=inactive"),
        }
    }


# ─── Main ─────────────────────────────────────────────────────────────────────

def _walk_for_key(obj, key):
    """Рекурсивный обход dict/list — выдаёт все значения по заданному ключу."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                yield v
            yield from _walk_for_key(v, key)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_for_key(v, key)


def _normalize_ad_record(raw: dict) -> dict:
    """Конвертит сырой GraphQL-ad record в плоский dict с нужными полями."""
    if not isinstance(raw, dict):
        return None
    try:
        s = raw.get("snapshot") or {}

        # Primary image: snapshot.images[0] (IMAGE) или cards[0] (DCO/DPA).
        # Prefer resized_image_url (обычно ~600px, 100-200KB, без watermark) —
        # original_image_url даёт full-res (1-2 MB), раздувает отчёт.
        # resized сохраняет качество для превью в отчёте.
        image_url = None
        images = s.get("images") or []
        if images:
            image_url = images[0].get("resized_image_url") or images[0].get("original_image_url")
        if not image_url:
            for card in (s.get("cards") or []):
                image_url = card.get("resized_image_url") or card.get("original_image_url")
                if image_url:
                    break

        # Video URL (HD приоритет). Заодно захватим video_preview_image_url как fallback image.
        video_url = None
        video_preview_image = None
        for v in (s.get("videos") or []):
            video_url = video_url or v.get("video_hd_url") or v.get("video_sd_url")
            video_preview_image = video_preview_image or v.get("video_preview_image_url")
            if video_url and video_preview_image:
                break
        if not video_url or not video_preview_image:
            for card in (s.get("cards") or []):
                video_url = video_url or card.get("video_hd_url") or card.get("video_sd_url")
                video_preview_image = video_preview_image or card.get("video_preview_image_url")
                if video_url and video_preview_image:
                    break

        # Если основной image_url пусто (VIDEO-only объявления) — используем preview кадр
        if not image_url and video_preview_image:
            image_url = video_preview_image

        # Body text — у DCO top-level это шаблон "{{product.brand}}".
        # Реальный рендеримый текст лежит в cards[i].body (может быть строкой или {text: ...}).
        def _is_tpl(t):
            return bool(t) and "{{" in t and "}}" in t

        body_text = ((s.get("body") or {}).get("text") or "").strip()
        if not body_text or _is_tpl(body_text):
            for card in (s.get("cards") or []):
                cb = card.get("body")
                if isinstance(cb, dict):
                    cb = cb.get("text")
                cb = (cb or "").strip()
                if cb and not _is_tpl(cb):
                    body_text = cb
                    break

        # Title — тоже может быть шаблоном у DCO, fallback на cards
        title = (s.get("title") or "").strip()
        if not title or _is_tpl(title):
            for card in (s.get("cards") or []):
                ct = (card.get("title") or "").strip()
                if ct and not _is_tpl(ct):
                    title = ct
                    break

        lib_id = str(raw.get("ad_archive_id") or "")
        if not lib_id:
            return None

        n_card_variants = len(s.get("cards") or [])

        return {
            "library_id":              lib_id,
            "page_name":               raw.get("page_name") or s.get("page_name") or "",
            "display_format":          s.get("display_format"),
            "is_active":               raw.get("is_active"),
            "start_date":              raw.get("start_date"),
            "end_date":                raw.get("end_date"),
            "platforms":               raw.get("publisher_platform") or [],
            "branded_content":         s.get("branded_content"),
            "title":                   title,
            "body_text":               body_text,
            "caption":                 s.get("caption") or "",
            "link_description":        s.get("link_description") or "",
            "link_url":                s.get("link_url") or "",
            "cta_text":                s.get("cta_text") or "",
            "cta_type":                s.get("cta_type") or "",
            "image_url":               image_url,
            "video_url":               video_url,
            "page_profile_uri":        s.get("page_profile_uri") or "",
            "page_profile_picture_url": s.get("page_profile_picture_url") or "",
            "page_like_count":         s.get("page_like_count"),
            "detail_url":              f"https://www.facebook.com/ads/library/?id={lib_id}",
            "n_card_variants":         n_card_variants,   # сколько carousel-вариантов
            # image_local заполнится позже в _download_ad_images
            "image_local":             None,
        }
    except Exception:
        return None


def _extract_ads_from_json(html: str, limit: int = 10,
                            target_page_id: str = None,
                            target_name: str = None) -> dict:
    """
    Вытаскивает структурированные ad records из Relay JSON payloads в HTML.
    FB Ads Library hydrate'ит страницу через GraphQL — данные доступны в
    <script type="application/json">...</script> блоках.

    Определяет mode по `ad_library_main.ad_library_page_info`:
      - non-null → advertiser-filtered (page mode) — результат авторитетный, без фильтра
      - null    → keyword search — шум возможен, нужен пост-фильтр

    Post-filter (в keyword mode):
      - target_page_id → точный match snapshot.page_id
      - target_name    → fuzzy match (difflib >= 0.85) snapshot.page_name
      - ни то ни то    → возвращаем как есть (unfiltered, 'keyword_raw')

    Threshold 0.85 (raised from 0.75 on 2026-05-07): difflib.SequenceMatcher
    Ratcliff/Obershelp ratio даёт ровно 0.75 для пар вида "X Locksmiths"/"Y
    Locksmiths" из-за общего суффикса (Grays Locksmiths UK прошёл фильтр для
    KeyMe Locksmiths search и попал в key.me/fb.json — confirmed validation
    bug). 0.85 блокирует такие пары, но пропускает legitimate variants
    (Joy Locksmith vs Joy Locksmith LLC = 0.87).

    Returns dict:
      {
        ads: [...],          # top-N matched ads
        mode: 'page' | 'keyword_filtered_by_page_id' | 'keyword_filtered_by_name' | 'keyword_raw',
        raw_total: int,      # search_results_connection.count (total FB returned)
        matched_count: int,  # after post-filter (== raw_total for page mode)
      }
    """
    all_ads = []
    seen_ids = set()
    page_mode_signal = None  # True если встретили ad_library_page_info != null
    raw_total = 0

    for m in re.finditer(
        r'<script type="application/json"[^>]*>(.+?)</script>',
        html, flags=re.DOTALL
    ):
        payload = m.group(1)
        try:
            doc = json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue

        # Mode signal: ad_library_page_info лежит рядом с ad_library_main (sibling),
        # а не внутри него. Ищем напрямую по дереву.
        if not page_mode_signal:
            for pi in _walk_for_key(doc, "ad_library_page_info"):
                if pi and isinstance(pi, dict) and pi.get("page_info"):
                    page_mode_signal = True
                    break

        # Ads из search_results_connection (независимо от mode)
        for conn in _walk_for_key(doc, "search_results_connection"):
            if not isinstance(conn, dict):
                continue
            c = conn.get("count")
            if isinstance(c, int) and c > raw_total:
                raw_total = c

            for edge in (conn.get("edges") or []):
                node = (edge or {}).get("node") or {}
                for raw_ad in (node.get("collated_results") or []):
                    ad = _normalize_ad_record(raw_ad)
                    if ad and ad["library_id"] not in seen_ids:
                        seen_ids.add(ad["library_id"])
                        # Временные поля для фильтрации — удалим перед возвратом
                        s = raw_ad.get("snapshot") or {}
                        ad["_snap_pid"] = str(s.get("page_id") or raw_ad.get("page_id") or "")
                        ad["_snap_pname"] = s.get("page_name") or ""
                        all_ads.append(ad)

    # Mode detection + post-filter
    # Inclusive: ad совпадает если ЛИБО page_id exact match ЛИБО fuzzy name match (или оба).
    # page-mode URL (view_all_page_id) не используется → page_mode_signal обычно False.
    if page_mode_signal is True:
        mode = "page"
        matched = all_ads
        matched_count = raw_total
    elif target_page_id or target_name:
        from difflib import SequenceMatcher
        def _norm(s): return (s or "").strip().lower()
        tgt_pid = str(target_page_id) if target_page_id else None
        tgt_name = _norm(target_name) if target_name else None

        matched = []
        for a in all_ads:
            pid_hit = bool(tgt_pid and a["_snap_pid"] == tgt_pid)
            name_hit = bool(tgt_name and
                            SequenceMatcher(None, _norm(a["_snap_pname"]), tgt_name).ratio() >= 0.85)
            if pid_hit or name_hit:
                matched.append(a)

        matched_count = len(matched)
        if tgt_pid and tgt_name:
            mode = "keyword_filtered_by_pid_or_name"
        elif tgt_pid:
            mode = "keyword_filtered_by_page_id"
        else:
            mode = "keyword_filtered_by_name"
    else:
        mode = "keyword_raw"
        matched = all_ads
        matched_count = len(matched)

    trimmed = matched[:limit]
    for a in trimmed:
        a.pop("_snap_pid", None)
        a.pop("_snap_pname", None)

    return {
        "ads": trimmed,
        "mode": mode,
        "raw_total": raw_total,
        "matched_count": matched_count,
    }


def _parse_ad_library_html(html: str, limit: int = 10,
                            target_page_id: str = None,
                            target_name: str = None) -> dict:
    """Парсит HTML страницы Ad Library. Возвращает count, structured_ads (post-filtered),
    тексты, partnership флаг, ads_library_mode, raw_keyword_total.

    Если передан target_page_id — фильтруем ads в keyword mode по нему.
    Иначе — если передан target_name — fuzzy-match фильтр по page_name.
    Если ни того ни другого — всё сырое.
    """
    result = {"count": None, "status": "could_not_parse", "ad_texts": [],
              "partnership_ads": False, "partnership_count": 0,
              "structured_ads": [], "ads_library_mode": "unknown",
              "raw_keyword_total": 0}

    # Count
    json_count = re.search(
        r'"search_results_connection"\s*:\s*\{[^}]*"count"\s*:\s*(\d+)', html
    )
    if json_count:
        result["count"] = int(json_count.group(1))
        result["status"] = "active" if result["count"] > 0 else "no_active_ads"
        result["method"] = "json"
    else:
        heading = re.search(r'~?(\d[\d,\s]*)\s+results?', html, re.IGNORECASE)
        if heading:
            try:
                count = int(re.sub(r'[,\s]', '', heading.group(1)))
                if 0 < count < 1000000:
                    result["count"] = count
                    result["status"] = "active"
                    result["method"] = "heading"
            except ValueError:
                pass
        if result["count"] is None:
            if any(s in html.lower() for s in ['no ads match', 'no results', '"edges":[]', '"count":0']):
                result["count"] = 0
                result["status"] = "no_active_ads"
                result["method"] = "empty_signal"

    # ── NEW: structured ads из Relay JSON ───────────────────────────
    extracted = _extract_ads_from_json(
        html, limit=limit,
        target_page_id=target_page_id, target_name=target_name
    )
    structured_ads = extracted["ads"]
    result["structured_ads"] = structured_ads
    result["ads_library_mode"] = extracted["mode"]
    result["raw_keyword_total"] = extracted["raw_total"]

    # Главное поле count — теперь это matched_count (после пост-фильтра),
    # а raw_keyword_total хранит исходное число из FB на случай transparency.
    #
    # Override count ТОЛЬКО когда extraction действительно отработал. Если
    # raw_total == 0 при том что regex выше нашёл count > 0 — это значит FB
    # hydrate'ит data ВНЕ <script type="application/json"> блоков (новые
    # версии UI), и наш _walk_for_key их не видит. В этом случае TRUST
    # regex-derived count, не затираем в 0. Иначе теряем валидные ads.
    extraction_succeeded = (
        extracted["mode"] == "page"
        or structured_ads
        or extracted["raw_total"] > 0  # FB вернул ads, post-filter killed all (legitimate)
    )
    if extraction_succeeded:
        result["count"] = extracted["matched_count"]
        result["status"] = "active" if extracted["matched_count"] > 0 else "no_active_ads"
        result["method"] = "json_" + extracted["mode"]

    if structured_ads:
        # Деривим плоский ad_texts список из отфильтрованных ads (backward compat)
        seen = set()
        unique_texts = []
        for ad in structured_ads:
            t = (ad.get("body_text") or "").strip()
            if t and t not in seen:
                seen.add(t)
                unique_texts.append(t)
        result["ad_texts"] = unique_texts
        result["partnership_count"] = sum(1 for a in structured_ads if a.get("branded_content"))
        result["partnership_ads"] = result["partnership_count"] > 0
        result["extraction_method"] = "json"
    elif extracted["raw_total"] == 0:
        # JSON нашёл 0 ads вообще — fallback на regex (redundant, но на всякий случай)
        texts = re.findall(r'white-space: pre-wrap[^>]*><span>([^<]{10,600})', html)
        seen = set()
        unique_texts = []
        for t in texts:
            t = t.strip()
            if t not in seen:
                seen.add(t)
                unique_texts.append(t)
        result["ad_texts"] = unique_texts
        partnership_count = len(re.findall(r'branded_content', html, re.IGNORECASE))
        estimated = max(0, partnership_count // 3)
        result["partnership_ads"] = estimated > 0
        result["partnership_count"] = estimated
        result["extraction_method"] = "regex_fallback"
    else:
        # JSON нашёл raw ads, но пост-фильтр отсёк все → честные нули
        # (не падаем в regex fallback — он считает шум от всех 109 advertiser'ов)
        result["ad_texts"] = []
        result["partnership_count"] = 0
        result["partnership_ads"] = False
        result["extraction_method"] = "json_all_filtered_out"

    return result


def _download_ad_images(domain: str, structured_ads: list,
                         status_label: str = "") -> list:
    """Скачивает главную картинку каждого ad record'а (по image_url из structured_ads).
    Имя файла: ad_{library_id}.jpg — для однозначной связи с текстом в fb.json.
    status_label: '' / 'active' / 'inactive' — подпапка для разделения 3-pass scan'a.
    Мутирует structured_ads: проставляет ad['image_local'] для HTML-репорта.
    Возвращает список путей скачанных файлов.

    NOTE: см. CLAUDE.md "Architectural backlog" — этот listing-side download
    запланирован к удалению; картинки должны приходить из detail modal scan
    (Step 5 fb_ad_modal_parse). Пока оставлено для совместимости.
    """
    import urllib.request
    from pathlib import Path

    img_dir = Path("scans") / domain / "fb_ads_images"
    if status_label:
        img_dir = img_dir / status_label
    img_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for ad in structured_ads:
        lib_id = ad.get("library_id")
        img_url = ad.get("image_url")
        if not (lib_id and img_url):
            continue
        try:
            ext = ".png" if ".png" in img_url.split("?")[0].lower() else ".jpg"
            filename = f"ad_{lib_id}{ext}"
            path = img_dir / filename

            req = urllib.request.Request(img_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()

            if len(data) < 5000:
                # Удаляем stale файл с прошлого rerun'а если он был — иначе
                # битый старый файл болтался бы рядом с обновлённым fb.json без
                # image_local (несоответствие). См. rerun edge case в comments.
                path.unlink(missing_ok=True)
                print(f"      ⚠️  Слишком маленький файл для ad {lib_id}, skip")
                continue

            path.write_bytes(data)
            # image_local — путь относительно scans/{domain}/, чтобы HTML репорт
            # мог легко подставить через relative src
            rel_dir = f"fb_ads_images/{status_label}/" if status_label else "fb_ads_images/"
            ad["image_local"] = f"{rel_dir}{filename}"
            saved.append(str(path))
            print(f"      📷 Сохранено: {filename}")
        except Exception as e:
            print(f"      ⚠️  Не удалось скачать ad {lib_id}: {str(e)[:60]}")

    return saved


def _build_ad_library_url(display_name: str, country: str, status: str) -> str:
    """status ∈ {'all','active','inactive'}. Keyword search only — view_all_page_id
    подтверждён мёртвым 2026-05-07, см. build_ads_library_urls."""
    keyword = display_name.strip().replace(" ", "%20")
    return (
        f"https://www.facebook.com/ads/library/"
        f"?active_status={status}&ad_type=all&country={country}"
        f"&is_targeted_country=false&media_type=all"
        f"&q={keyword}"
        f"&search_type=keyword_unordered"
        f"&sort_data[direction]=desc&sort_data[mode]=total_impressions"
    )


def _scan_one_status(page, url: str, status_label: str, domain: str,
                      display_name: str, download_images: bool,
                      target_page_id: str = None,
                      target_name: str = None) -> dict:
    """Открывает URL, парсит HTML, скачивает картинки, сохраняет тексты.
    status_label: 'all' / 'active' / 'inactive'.

    target_page_id / target_name — фильтры для _parse_ad_library_html.
    Если переданы — keyword search post-filter активен (по page_id exact OR fuzzy name).
    Если None — keyword_raw mode (без фильтра, для backward-compat вызовов)."""
    try:
        page.goto(url, wait_until="networkidle", timeout=25000)
        page.wait_for_timeout(3000)
    except Exception:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(4000)
        except Exception:
            pass

    html = page.content()
    parsed = _parse_ad_library_html(html,
                                     target_page_id=target_page_id,
                                     target_name=target_name)
    parsed["search_term"] = display_name
    parsed["status_filter"] = status_label

    # Скачиваем изображения только для active/inactive (не для all — он только для счётчика)
    # Источник URL'ов — structured_ads из _parse_ad_library_html, не DOM scrape.
    saved_images = []
    if download_images and domain and status_label in ("active", "inactive"):
        structured_ads = parsed.get("structured_ads") or []
        if parsed.get("count", 0) > 0 and structured_ads:
            print(f"      📷 Скачиваю изображения [{status_label}]...")
            # Мутирует structured_ads — проставляет image_local
            saved_images = _download_ad_images(domain, structured_ads,
                                                status_label=status_label)
    parsed["saved_images"] = saved_images

    # Сохраняем тексты с суффиксом
    if domain and parsed.get("ad_texts") and status_label in ("active", "inactive"):
        from pathlib import Path
        texts_dir = Path("scans") / domain / "fb_ads_images"
        texts_dir.mkdir(parents=True, exist_ok=True)
        texts_path = texts_dir / f"ad_texts_{status_label}.txt"
        with open(texts_path, "w", encoding="utf-8") as f:
            f.write(f"Ad texts for: {display_name} [{status_label}]\n")
            f.write(f"Total: {len(parsed['ad_texts'])}\n")
            f.write("=" * 60 + "\n\n")
            for i, text in enumerate(parsed["ad_texts"], 1):
                f.write(f"[{i}]\n{text}\n\n")
        print(f"      📄 Тексты [{status_label}]: {texts_path}")
        parsed["saved_texts_path"] = str(texts_path)

    return parsed


def get_ads_data(display_name: str, page_id: str = None,
                  country: str = "ALL", fb_page_url: str = None,
                  domain: str = "", download_images: bool = True) -> dict:
    """
    3-проходный скан Ad Library (только LISTING — без deep-scan модалок):
      1) active_status=all → есть ли что-то вообще
      2) если есть — active_status=active
      3) если total > active — active_status=inactive
    Возвращает {total_ever, active, inactive, и back-compat поля}.

    Filter activation: page_id и display_name пробрасываются в каждый
    _scan_one_status → _parse_ad_library_html → _extract_ads_from_json для
    post-filter (page_id exact OR fuzzy name >= 0.75). Если page_id None —
    останется только name-filter; если оба None — keyword_raw mode (шум).

    Deep-scan модалок (Reach/демография/disclaimer/advertiser/lead-form) живёт
    в отдельном модуле — см. fb_scan.py (orchestrator) и Modules 3/4/5.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"total_ever": None, "error": "playwright not installed"}

    result = {
        "total_ever": None,
        "active": None,
        "inactive": None,
        "search_term": display_name,
        "country": country,
    }

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
                locale="en-US",
            )
            page = context.new_page()

            # ── Pass 1: total ever (active_status=all) ─────────────────
            print(f"      🔎 Проход 1/3: все объявления (active+inactive)...")
            url_all = _build_ad_library_url(display_name, country, "all")
            all_data = _scan_one_status(page, url_all, "all", domain,
                                         display_name, download_images=False,
                                         target_page_id=page_id,
                                         target_name=display_name)
            total = all_data.get("count")
            result["total_ever"] = total

            if not total or total == 0:
                print(f"      ❌ Объявлений не найдено вообще")
                browser.close()
                return result

            print(f"      📊 Всего объявлений (когда-либо): {total}")

            # ── Pass 2: active only ────────────────────────────────────
            print(f"      🔎 Проход 2/3: активные объявления...")
            url_active = _build_ad_library_url(display_name, country, "active")
            active_data = _scan_one_status(page, url_active, "active", domain,
                                            display_name, download_images,
                                            target_page_id=page_id,
                                            target_name=display_name)
            active_count = active_data.get("count", 0) or 0
            if active_count > 0:
                result["active"] = active_data
                print(f"      ✅ Активных: {active_count}")
            else:
                print(f"      ➖ Активных нет (но есть в архиве)")

            # ── Pass 3: inactive only (только если есть смысл) ────────
            if total > active_count:
                print(f"      🔎 Проход 3/3: неактивные объявления...")
                url_inactive = _build_ad_library_url(display_name, country, "inactive")
                inactive_data = _scan_one_status(page, url_inactive, "inactive", domain,
                                                  display_name, download_images,
                                                  target_page_id=page_id,
                                                  target_name=display_name)
                inactive_count = inactive_data.get("count", 0) or 0
                if inactive_count > 0:
                    result["inactive"] = inactive_data
                    print(f"      📦 Неактивных: {inactive_count}")
            else:
                print(f"      ➖ Все объявления активны — inactive скан не нужен")

            browser.close()

        # ── Back-compat: добавляем плоские поля чтобы старые консьюмеры работали ─
        active = result.get("active") or {}
        inactive = result.get("inactive") or {}

        # Тексты — объединение (active в приоритете)
        combined_texts = list(active.get("ad_texts") or [])
        for t in (inactive.get("ad_texts") or []):
            if t not in combined_texts:
                combined_texts.append(t)

        # Картинки — объединение (отдельные подпапки уже разделены)
        combined_images = list(active.get("saved_images") or []) + \
                          list(inactive.get("saved_images") or [])

        # Partnership — OR / sum
        partnership = bool(active.get("partnership_ads")) or bool(inactive.get("partnership_ads"))
        partnership_n = (active.get("partnership_count") or 0) + (inactive.get("partnership_count") or 0)

        # Combined structured_ads — active первыми, потом inactive (dedup by library_id).
        # Нужно для generate_site_report.py / generate_batch_report.py — они ждут
        # blissful-style flat schema на корне fb account record'a.
        combined_structured = list(active.get("structured_ads") or [])
        seen_ids = {a.get("library_id") for a in combined_structured if a.get("library_id")}
        for a in (inactive.get("structured_ads") or []):
            lib_id = a.get("library_id")
            if lib_id and lib_id not in seen_ids:
                combined_structured.append(a)
                seen_ids.add(lib_id)

        result.update({
            "count": active.get("count") or 0,            # back-compat: count = active count
            "ad_texts": combined_texts,
            "saved_images": combined_images,
            "partnership_ads": partnership,
            "partnership_count": partnership_n,
            # NEW flat fields (для generate_site_report / generate_batch_report):
            "structured_ads":     combined_structured,
            "ads_library_mode":   active.get("ads_library_mode") or inactive.get("ads_library_mode") or "unknown",
            "raw_keyword_total":  max(active.get("raw_keyword_total") or 0,
                                      inactive.get("raw_keyword_total") or 0),
            "extraction_method":  active.get("extraction_method") or inactive.get("extraction_method") or "unknown",
        })

        return result

    except Exception as e:
        return {"total_ever": None, "error": str(e)[:100],
                "search_term": display_name, "country": country}


# Back-compat alias — старые вызовы не сломаются
get_active_ads_count = get_ads_data
