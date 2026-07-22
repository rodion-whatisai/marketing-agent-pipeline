"""
TNC — Facebook Ads Library Scraper
==================================
Скрапит Facebook Ads Library. URL builder выбирает стратегию по типу page_id:

  - Classic Page ID (16-значный 1xxx…, _is_classic_page_id) → view_all_page_id
    URL (page-mode). Verified working: redacted prospect 1234567890123456 (2026-05-11).
  - New-style ID (100063xxx / 61xxx) → keyword search + post-filter по
    snapshot.page_id (exact) и snapshot.page_name (fuzzy >= 0.85).
    view_all_page_id endpoint мёртв для new-style с 2026-05-07 (KeyMe Locksmiths).
  - Без page_id → keyword search + fuzzy name filter.

Post-filter threshold 0.85 поднят с 0.75 после false positive Grays Locksmiths
vs KeyMe Locksmiths (общий суффикс давал 0.75).

Содержит:
  - _is_classic_page_id: классификатор Page ID (classic vs new-style)
  - build_ads_library_urls: URL builder (active / all / inactive)
  - _walk_for_key / _normalize_ad_record / _extract_ads_from_json /
    _parse_ad_library_html: HTML/JSON парсинг
  - _download_ad_images: скачивание creative картинок
  - _build_ad_library_url / _scan_one_status: per-status scan
  - get_ads_data: 3-проходный listing scan (orchestrator)

Используется: fb_page_id.py (orchestrator), fb_ads_listing.py, fb_page_finder.py.

Известная архитектурная проблема (см. CLAUDE.md):
  - _extract_ads_from_json ищет JSON только в <script type="application/json">.
    Новые версии FB UI могут hydrate'ить вне этого тега → structured_ads пустой,
    count работает только через regex fallback.
"""

import re
import json
import ssl
from urllib.parse import urlparse

from utils import scan_path, setup_console
from log import log_info, log_warn, log_error, log_success, log_step, log_debug


def _ssl_unverified_ctx():
    """SSL-контекст без проверки сертификата для urllib (см. utils: TNC_SSL_VERIFY).
    Скачивание картинок объявлений — read-only, секретов не передаём."""
    import os
    if os.environ.get("TNC_SSL_VERIFY") == "1":
        return None  # строгий режим — дефолтная проверка
    return ssl._create_unverified_context()
setup_console()


# ─── Page ID классификатор ────────────────────────────────────────────────────

def _is_classic_page_id(page_id) -> bool:
    """Classic FB Page ID — 13-17-значное число формата 1xxx…,
    НЕ начинающееся с 100063 (new-style Page) или 61 (profile / new-style).

    Зачем: view_all_page_id URL в Ad Library работает только для classic IDs.
    Для new-style — endpoint deprecated, возвращает 0 даже залогиненному.
    См. CLAUDE.md "view_all_page_id" + memory project_view_all_page_id_dead.

    Тест-кейсы:
      "1234567890123456" (redacted prospect, 16 цифр, prefix 1)  → True
      "100063757071484"  (KeyMe, prefix 100063)             → False
      "61500000000000"   (Aster profile, prefix 61)         → False
      None / "" / "abc"                                      → False
    """
    if not page_id:
        return False
    pid = str(page_id).strip()
    if not pid.isdigit():
        return False
    if pid.startswith("100063") or pid.startswith("61"):
        return False
    return 13 <= len(pid) <= 17 and pid[0] == "1"


# ─── Ads Library URLs ─────────────────────────────────────────────────────────

def build_ads_library_urls(display_name: str, countries: list = None,
                            page_id: str = None) -> dict:
    """Строит ссылки на Ad Library (active / all / inactive).

    Стратегия (см. _build_ad_library_url):
      - page_id classic (16-значный 1xxx…) → view_all_page_id URL (page-mode).
      - new-style / без page_id → keyword search + post-filter в
        _extract_ads_from_json (snapshot.page_id exact OR fuzzy name >= 0.85).

    countries параметр для back-compat — игнорируется (всегда country=ALL,
    фильтрация по странам идёт через site_country отдельно).
    """
    return {
        "ALL": {
            "active_only":   _build_ad_library_url(display_name, "ALL", "active",   page_id=page_id),
            "all":           _build_ad_library_url(display_name, "ALL", "all",      page_id=page_id),
            "inactive_only": _build_ad_library_url(display_name, "ALL", "inactive", page_id=page_id),
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
        log_debug("_normalize_ad_record: raw не dict — skip")
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
            log_debug("_normalize_ad_record: нет ad_archive_id — skip")
            return None

        n_card_variants = len(s.get("cards") or [])
        log_debug(f"_normalize_ad_record: lib_id={lib_id} cards={n_card_variants} image={bool(image_url)} video={bool(video_url)}")

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
    except Exception as e:
        log_debug(f"_normalize_ad_record: исключение при нормализации записи: {e}")
        return None


def _strip_www(host: str) -> str:
    return host[4:] if host.startswith("www.") else host


def _ad_leads_to_domain(ad: dict, domain: str) -> bool:
    """Ведёт ли объявление на сайт клиента (link_url/caption → домен).

    Якорь идентификации бренда: домен известен ДО любого поиска в Ads Library,
    в отличие от имени страницы (одноимённые импостеры) и page_id (появляется
    только из выдачи). Tested: 2026-07-22 on plurio.ai — 13 ads → plurio.ai,
    1 ad импостера (page 'Plurio' @plurioid) → instagram.com/plurioid."""
    d = _strip_www((domain or "").strip().lower())
    if not d:
        return False
    for field in (ad.get("link_url"), ad.get("caption")):
        if not field:
            continue
        t = str(field).strip().lower()
        host = urlparse(t if "://" in t else "http://" + t).netloc or t.split("/")[0]
        host = _strip_www(host.split(":")[0])
        if host == d or host.endswith("." + d):
            return True
    return False


def _extract_ads_from_json(html: str, limit: int = 10,
                            target_page_id: str = None,
                            target_name: str = None,
                            target_domain: str = None) -> dict:
    """
    Вытаскивает структурированные ad records из Relay JSON payloads в HTML.
    FB Ads Library hydrate'ит страницу через GraphQL — данные доступны в
    <script type="application/json">...</script> блоках.

    Определяет mode по `ad_library_main.ad_library_page_info`:
      - non-null → advertiser-filtered (page mode) — результат авторитетный, без фильтра
      - null    → keyword search — шум возможен, нужен пост-фильтр

    Post-filter (в keyword mode), по убыванию приоритета:
      - target_domain  → доменный якорь: страницы, чьи ads ведут на домен клиента
                         (link_url/caption) → 'keyword_filtered_by_domain'.
                         Отсеивает одноимённых импостеров (fuzzy-имя бессильно).
      - target_page_id → точный match snapshot.page_id
      - target_name    → fuzzy match (difflib >= 0.85) snapshot.page_name
      - ничего         → возвращаем как есть (unfiltered, 'keyword_raw')

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
    log_debug(f"_extract_ads_from_json: вход — html={len(html)} bytes, limit={limit}, "
              f"target_page_id={target_page_id}, target_name={target_name}")
    all_ads = []
    seen_ids = set()
    page_mode_signal = None  # True если встретили ad_library_page_info != null
    raw_total = 0

    script_blocks = 0
    for m in re.finditer(
        r'<script type="application/json"[^>]*>(.+?)</script>',
        html, flags=re.DOTALL
    ):
        script_blocks += 1
        payload = m.group(1)
        try:
            doc = json.loads(payload)
        except (json.JSONDecodeError, ValueError) as e:
            log_debug(f"_extract_ads_from_json: JSON-блок не распарсился, skip: {e}")
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

    log_debug(f"_extract_ads_from_json: просканировано {script_blocks} JSON-блоков, "
              f"raw_total={raw_total}, собрано {len(all_ads)} ad records, "
              f"page_mode_signal={page_mode_signal}")

    # Mode detection + post-filter
    # Inclusive: ad совпадает если ЛИБО page_id exact match ЛИБО fuzzy name match (или оба).
    # page-mode URL (view_all_page_id) не используется → page_mode_signal обычно False.
    brand_page_uris = []
    if page_mode_signal is True:
        log_debug("_extract_ads_from_json: ветка page-mode — фильтр не применяется")
        mode = "page"
        matched = all_ads
        matched_count = raw_total
    elif target_domain and any(
            a.get("page_profile_uri") and _ad_leads_to_domain(a, target_domain)
            for a in all_ads):
        # ── Доменный якорь (приоритетнее fuzzy-имени) ────────────────────
        # Страница считается клиентской, если хоть одно её объявление ведёт на
        # домен клиента. Одноимённые импостеры (page_name идентичен, fuzzy=1.0)
        # отсеиваются: их объявления ведут в другие места.
        client_pages = {a["page_profile_uri"] for a in all_ads
                        if a.get("page_profile_uri") and _ad_leads_to_domain(a, target_domain)}
        matched = [a for a in all_ads if a.get("page_profile_uri") in client_pages]
        matched_count = len(matched)
        mode = "keyword_filtered_by_domain"
        brand_page_uris = sorted(client_pages)
        dropped = len(all_ads) - matched_count
        log_debug(f"_extract_ads_from_json: доменный фильтр '{target_domain}' — "
                  f"клиентских страниц {len(client_pages)} ({brand_page_uris}), "
                  f"matched={matched_count}/{len(all_ads)}, отсеяно {dropped}")
    elif target_page_id or target_name:
        log_debug("_extract_ads_from_json: ветка keyword post-filter (по page_id и/или name)")
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
            else:
                log_debug(f"_extract_ads_from_json: отфильтрован ad {a.get('library_id')} "
                          f"(pid={a['_snap_pid']!r}, name={a['_snap_pname']!r})")

        matched_count = len(matched)
        if tgt_pid and tgt_name:
            mode = "keyword_filtered_by_pid_or_name"
        elif tgt_pid:
            mode = "keyword_filtered_by_page_id"
        else:
            mode = "keyword_filtered_by_name"
        log_debug(f"_extract_ads_from_json: post-filter mode={mode}, matched={matched_count}/{len(all_ads)}")
    else:
        log_debug("_extract_ads_from_json: ветка keyword_raw — без фильтра")
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
        # Клиентские FB-страницы по доменному якорю (пусто вне domain-режима).
        # Источник для fb.json account.page_url и моста к deep-scan (fb_page_finder).
        "brand_page_uris": brand_page_uris,
        "scraped_count": len(all_ads),
    }


def _parse_ad_library_html(html: str, limit: int = 10,
                            target_page_id: str = None,
                            target_name: str = None,
                            target_domain: str = None) -> dict:
    """Парсит HTML страницы Ad Library. Возвращает count, structured_ads (post-filtered),
    тексты, partnership флаг, ads_library_mode, raw_keyword_total, brand_page_uris.

    Приоритет фильтров keyword mode: target_domain (доменный якорь) →
    target_page_id (exact) → target_name (fuzzy). Ничего не передали — всё сырое.
    """
    log_debug(f"_parse_ad_library_html: вход — html={len(html)} bytes, limit={limit}, "
              f"target_page_id={target_page_id}, target_name={target_name}, "
              f"target_domain={target_domain}")
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
        log_debug(f"_parse_ad_library_html: count из JSON regex = {result['count']}")
    else:
        heading = re.search(r'~?(\d[\d,\s]*)\s+results?', html, re.IGNORECASE)
        if heading:
            try:
                count = int(re.sub(r'[,\s]', '', heading.group(1)))
                if 0 < count < 1000000:
                    result["count"] = count
                    result["status"] = "active"
                    result["method"] = "heading"
                    log_debug(f"_parse_ad_library_html: count из heading regex = {count}")
            except ValueError as e:
                log_debug(f"_parse_ad_library_html: heading count не парсится: {e}")
        if result["count"] is None:
            if any(s in html.lower() for s in ['no ads match', 'no results', '"edges":[]', '"count":0']):
                result["count"] = 0
                result["status"] = "no_active_ads"
                result["method"] = "empty_signal"
                log_debug("_parse_ad_library_html: count=0 по empty_signal")

    # ── NEW: structured ads из Relay JSON ───────────────────────────
    extracted = _extract_ads_from_json(
        html, limit=limit,
        target_page_id=target_page_id, target_name=target_name,
        target_domain=target_domain
    )
    structured_ads = extracted["ads"]
    result["structured_ads"] = structured_ads
    result["ads_library_mode"] = extracted["mode"]
    result["raw_keyword_total"] = extracted["raw_total"]
    result["brand_page_uris"] = extracted.get("brand_page_uris") or []

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
        log_debug(f"_parse_ad_library_html: extraction отработал — override count = "
                  f"{extracted['matched_count']} (mode={extracted['mode']})")
        result["count"] = extracted["matched_count"]
        result["status"] = "active" if extracted["matched_count"] > 0 else "no_active_ads"
        result["method"] = "json_" + extracted["mode"]
    else:
        log_debug("_parse_ad_library_html: extraction пуст — trust regex-derived count, не затираем")

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
        log_debug(f"_parse_ad_library_html: extraction_method=json — "
                  f"{len(unique_texts)} текстов, partnership={result['partnership_count']}")
    elif extracted["raw_total"] == 0:
        log_debug("_parse_ad_library_html: structured_ads пуст и raw_total=0 — regex fallback")
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
        log_debug(f"_parse_ad_library_html: extraction_method=regex_fallback — "
                  f"{len(unique_texts)} текстов")
    else:
        # JSON нашёл raw ads, но пост-фильтр отсёк все → честные нули
        # (не падаем в regex fallback — он считает шум от всех 109 advertiser'ов)
        log_debug("_parse_ad_library_html: raw ads найдены, но post-filter отсёк все → "
                  "extraction_method=json_all_filtered_out")
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

    log_debug(f"_download_ad_images: вход — domain={domain}, "
              f"{len(structured_ads)} ads, status_label={status_label!r}")
    img_dir = Path("scans") / domain / "fb_ads_images"
    if status_label:
        img_dir = img_dir / status_label
    img_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for ad in structured_ads:
        lib_id = ad.get("library_id")
        img_url = ad.get("image_url")
        if not (lib_id and img_url):
            log_debug(f"_download_ad_images: пропуск ad {lib_id} — нет library_id или image_url")
            continue
        try:
            log_debug(f"_download_ad_images: качаю ad {lib_id} из {img_url[:80]}")
            ext = ".png" if ".png" in img_url.split("?")[0].lower() else ".jpg"
            filename = f"ad_{lib_id}{ext}"
            path = img_dir / filename

            req = urllib.request.Request(img_url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            })
            with urllib.request.urlopen(req, timeout=15,
                                        context=_ssl_unverified_ctx()) as r:
                data = r.read()

            if len(data) < 5000:
                # Удаляем stale файл с прошлого rerun'а если он был — иначе
                # битый старый файл болтался бы рядом с обновлённым fb.json без
                # image_local (несоответствие). См. rerun edge case в comments.
                path.unlink(missing_ok=True)
                log_warn(f"Слишком маленький файл для ad {lib_id}, skip")
                continue

            path.write_bytes(data)
            # image_local — путь относительно scans/{domain}/, чтобы HTML репорт
            # мог легко подставить через relative src
            rel_dir = f"fb_ads_images/{status_label}/" if status_label else "fb_ads_images/"
            ad["image_local"] = f"{rel_dir}{filename}"
            saved.append(str(path))
            log_success(f"Сохранено: {filename}", emoji="📷")
        except Exception as e:
            log_warn(f"Не удалось скачать ad {lib_id}: {str(e)[:60]}")

    return saved


def _build_ad_library_url(display_name: str, country: str, status: str,
                           page_id: str = None) -> str:
    """status ∈ {'all','active','inactive'}.

    Стратегия:
      - Classic Page ID (см. _is_classic_page_id) → view_all_page_id URL
        (page-mode, без шума, без post-filter). Verified: redacted prospect
        1234567890123456.
      - Иначе (new-style ID или None) → keyword search + post-filter в
        _extract_ads_from_json. view_all_page_id для new-style мёртв
        (CLAUDE.md, 2026-05-07).
    """
    if _is_classic_page_id(page_id):
        log_debug(f"_build_ad_library_url: classic page_id={page_id}, status={status} "
                  f"→ view_all_page_id URL (page-mode)")
        return (
            f"https://www.facebook.com/ads/library/"
            f"?active_status={status}&ad_type=all&country={country}"
            f"&view_all_page_id={page_id}"
            f"&search_type=page&media_type=all"
        )
    log_debug(f"_build_ad_library_url: new-style/без page_id (page_id={page_id}), status={status} "
              f"→ keyword search URL для '{display_name}'")
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
    log_debug(f"_scan_one_status: status={status_label!r}, domain={domain}, "
              f"download_images={download_images}, target_page_id={target_page_id}, url={url}")
    try:
        page.goto(url, wait_until="networkidle", timeout=25000)
        page.wait_for_timeout(3000)
    except Exception as e:
        log_debug(f"_scan_one_status: networkidle goto не прошёл ({e}), fallback на domcontentloaded")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=15000)
            page.wait_for_timeout(4000)
        except Exception as e:
            log_debug(f"_scan_one_status: domcontentloaded goto тоже не прошёл: {e}")

    html = page.content()
    parsed = _parse_ad_library_html(html,
                                     target_page_id=target_page_id,
                                     target_name=target_name,
                                     target_domain=domain)
    parsed["search_term"] = display_name
    parsed["status_filter"] = status_label

    # Скачиваем изображения только для active/inactive (не для all — он только для счётчика)
    # Источник URL'ов — structured_ads из _parse_ad_library_html, не DOM scrape.
    saved_images = []
    if download_images and domain and status_label in ("active", "inactive"):
        structured_ads = parsed.get("structured_ads") or []
        if parsed.get("count", 0) > 0 and structured_ads:
            log_step(f"Скачиваю изображения [{status_label}]...", emoji="📷")
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
        log_success(f"Тексты [{status_label}]: {texts_path}", emoji="📄")
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
    log_debug(f"get_ads_data: вход — display_name={display_name!r}, page_id={page_id}, "
              f"country={country}, domain={domain}, download_images={download_images}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        log_error(f"playwright не установлен: {e}")
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
            log_step(f"Проход 1/3: все объявления (active+inactive)...", emoji="🔎")
            url_all = _build_ad_library_url(display_name, country, "all", page_id=page_id)
            all_data = _scan_one_status(page, url_all, "all", domain,
                                         display_name, download_images=False,
                                         target_page_id=page_id,
                                         target_name=display_name)
            total = all_data.get("count")
            result["total_ever"] = total

            if not total or total == 0:
                log_error(f"Объявлений не найдено вообще")
                browser.close()
                return result

            log_info(f"Всего объявлений (когда-либо): {total}")

            # ── Pass 2: active only ────────────────────────────────────
            log_step(f"Проход 2/3: активные объявления...", emoji="🔎")
            url_active = _build_ad_library_url(display_name, country, "active", page_id=page_id)
            active_data = _scan_one_status(page, url_active, "active", domain,
                                            display_name, download_images,
                                            target_page_id=page_id,
                                            target_name=display_name)
            active_count = active_data.get("count", 0) or 0
            if active_count > 0:
                result["active"] = active_data
                log_success(f"Активных: {active_count}")
            else:
                log_info(f"Активных нет (но есть в архиве)")

            # ── Pass 3: inactive only (только если есть смысл) ────────
            if total > active_count:
                log_step(f"Проход 3/3: неактивные объявления...", emoji="🔎")
                url_inactive = _build_ad_library_url(display_name, country, "inactive", page_id=page_id)
                inactive_data = _scan_one_status(page, url_inactive, "inactive", domain,
                                                  display_name, download_images,
                                                  target_page_id=page_id,
                                                  target_name=display_name)
                inactive_count = inactive_data.get("count", 0) or 0
                if inactive_count > 0:
                    result["inactive"] = inactive_data
                    log_info(f"Неактивных: {inactive_count}")
            else:
                log_info(f"Все объявления активны — inactive скан не нужен")

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
            # Клиентские FB-страницы по доменному якорю (union всех трёх проходов).
            "brand_page_uris":    sorted(set(all_data.get("brand_page_uris") or []) |
                                         set(active.get("brand_page_uris") or []) |
                                         set(inactive.get("brand_page_uris") or [])),
        })

        return result

    except Exception as e:
        log_error(f"get_ads_data: скан Ad Library упал: {str(e)[:100]}")
        return {"total_ever": None, "error": str(e)[:100],
                "search_term": display_name, "country": country}


# Back-compat alias — старые вызовы не сломаются
get_active_ads_count = get_ads_data
