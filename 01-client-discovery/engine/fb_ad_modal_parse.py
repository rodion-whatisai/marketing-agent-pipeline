"""
Module 5: Чистый парсер модалки FB Ad Library → структурированный dict.
БЕЗ браузера. Принимает HTML строку модалки (с раскрытыми dropdowns или нет).

Парсит до 5 секций, каждая необязательная — некоторые объявления имеют только подмножество:
  - meta: library_id, started_running, body, multiple_versions
  - transparency: total_reach, demographics[], age_range, gender_target, country_targets
  - disclaimer: location, website, advertiser, payer
  - advertiser_meta: name, handle, followers, more_info
  - additional_assets: links[], text_items[]

Использование:
    from fb_ad_modal_parse import parse_modal, detect_sections
    sections = detect_sections(html)         # → ['Transparency by location', 'About the advertiser', ...]
    data = parse_modal(html)                 # → {meta, transparency, disclaimer, advertiser, additional_assets}
"""
import re

from log import log_warn, log_debug, log_header

ALL_SECTION_LABELS = [
    "Transparency by location",
    "About the disclaimer",
    "About the advertiser",
    "Advertiser and payer",
    "Additional assets from this ad",
]


# ─── Утилиты ────────────────────────────────────────────────────────────────

def _strip(html: str) -> str:
    """Убирает теги, схлопывает пробелы."""
    t = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", t).strip()


def detect_sections(html: str) -> list:
    """Возвращает список секций которые присутствуют в DOM (по наличию heading-текста)."""
    found = [lbl for lbl in ALL_SECTION_LABELS if lbl in html]
    log_debug(f"detect_sections: найдено {len(found)}/{len(ALL_SECTION_LABELS)} секций: {found}")
    return found


# ─── Meta (всегда присутствует) ─────────────────────────────────────────────

def _parse_library_id(html: str) -> str:
    m = re.search(r"Library ID:\s*(\d+)", html)
    return m.group(1) if m else ""


def _parse_started_running(html: str) -> str:
    m = re.search(r"Started running on\s*([A-Za-z]+ \d+, \d{4})", html)
    return m.group(1) if m else ""


def _parse_multiple_versions(html: str) -> int:
    m = re.search(r"(\d+)\s+of\s+(\d+)", html)
    return int(m.group(2)) if m else 1


def _parse_body(html: str) -> str:
    """Текст самого объявления — первый <span> в pre-wrap."""
    m = re.search(r'white-space:\s*pre-wrap[^>]*>\s*<span[^>]*>([^<]{5,2000})', html)
    return m.group(1).strip() if m else ""


def _parse_meta(html: str) -> dict:
    log_debug("_parse_meta: парсинг library_id/started_running/versions/body")
    out = {
        "library_id":          _parse_library_id(html),
        "started_running":     _parse_started_running(html),
        "multiple_versions":   _parse_multiple_versions(html),
        "body":                _parse_body(html),
    }
    log_debug(f"_parse_meta: library_id={out['library_id']!r}, started={out['started_running']!r}, "
              f"versions={out['multiple_versions']}, body_len={len(out['body'])}")
    return out


# ─── Transparency by location ───────────────────────────────────────────────

def _parse_transparency(html: str) -> dict:
    out = {"total_reach": None, "demographics": [],
           "age_range": "", "gender_target": "", "country_targets": []}

    log_debug("_parse_transparency: вход")
    i = html.find("Transparency by location")
    if i < 0:
        log_debug("_parse_transparency: секция 'Transparency by location' отсутствует — пустой результат")
        return out
    section = html[i:i + 100000]

    # Total Reach — между "Reach" заголовком и "Reach by location, age and gender"
    m = re.search(r"(?<![a-zA-Z])Reach(?![a-zA-Z]).{0,3000}?Reach by location",
                   section, re.DOTALL)
    if m:
        nums = re.findall(r">([\d,]{2,15})<", m.group(0))
        nums = [n for n in nums if "," in n or len(n) >= 3]
        if nums:
            try:
                out["total_reach"] = int(nums[0].replace(",", ""))
                log_debug(f"_parse_transparency: total_reach={out['total_reach']}")
            except ValueError as e:
                log_debug(f"_parse_transparency: total_reach parse failed на {nums[0]!r}: {e}")

    # Демографическая таблица: Location | Age Range | Gender | Reach
    j = section.find("Age Range")
    if j > 0:
        chunk = section[j - 100:j + 30000]
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", chunk, re.DOTALL)
        clean = [_strip(c) for c in cells if c.strip()]
        clean = [c for c in clean if c and len(c) < 100]
        if len(clean) >= 8 and clean[0] == "Location":
            for k in range(4, len(clean) - 3, 4):
                row = clean[k:k + 4]
                if len(row) == 4:
                    try:
                        reach = int(row[3].replace(",", ""))
                    except ValueError as e:
                        log_debug(f"_parse_transparency: skip row {row} — reach не число: {e}")
                        continue
                    out["demographics"].append({
                        "location": row[0],
                        "age": row[1],
                        "gender": row[2],
                        "reach": reach,
                    })
                    if row[0] not in out["country_targets"]:
                        out["country_targets"].append(row[0])

    # Age range
    m = re.search(r"(\d+-\d+\+?)\s+years old", section)
    if m: out["age_range"] = m.group(1)

    # Gender — All / Male / Female
    m = re.search(r">Gender<.{0,500}?>(All|Male|Female|Men|Women)<",
                   section, re.DOTALL)
    if m: out["gender_target"] = m.group(1)
    log_debug(f"_parse_transparency: demographics={len(out['demographics'])}, "
              f"countries={out['country_targets']}, age={out['age_range']!r}, gender={out['gender_target']!r}")
    return out


# ─── Disclaimer ─────────────────────────────────────────────────────────────

def _parse_disclaimer(html: str) -> dict:
    out = {"location": "", "website": "", "advertiser": "", "payer": ""}
    log_debug("_parse_disclaimer: вход")
    i = html.find("About the disclaimer")
    if i < 0:
        log_debug("_parse_disclaimer: секция 'About the disclaimer' отсутствует — пустой результат")
        return out
    section = html[i:i + 8000]
    text = _strip(section)

    m = re.search(r"Location\s+([A-Z][A-Za-z ]{2,40}?)(?=\s+Website|\s+Advertiser|$)", text)
    if m: out["location"] = m.group(1).strip()
    m = re.search(r"Website\s+(https?://\S+)", text)
    if m: out["website"] = m.group(1).rstrip("/")
    m = re.search(r"Advertiser\s+([A-Z][A-Za-z0-9 .,&\-]{2,80}?)(?=\s+Payer|\s+About|$)", text)
    if m: out["advertiser"] = m.group(1).strip()
    m = re.search(r"Payer\s+([A-Z][A-Za-z0-9 .,&\-]{2,80}?)(?=\s+About|$)", text)
    if m: out["payer"] = m.group(1).strip()
    log_debug(f"_parse_disclaimer: {out}")
    return out


# ─── About the advertiser (page meta) ───────────────────────────────────────

def _parse_followers(text: str):
    """Парсит '15.3K followers' / '113 followers' / '1.2M followers' → int."""
    # Берём первое явное число + suffix (K/M) ПЕРЕД словом 'followers'
    m = re.search(r"([\d,]+(?:\.\d+)?[KkMm]?)\s+followers?", text)
    if not m:
        log_debug("_parse_followers: 'followers' не найдено")
        return None
    v = m.group(1).replace(",", "")
    try:
        if v.endswith(("K", "k")):
            return int(float(v[:-1]) * 1000)
        if v.endswith(("M", "m")):
            return int(float(v[:-1]) * 1_000_000)
        return int(float(v))
    except ValueError as e:
        log_debug(f"_parse_followers: не смог распарсить {v!r}: {e}")
        return None


def _parse_advertiser_meta(html: str) -> dict:
    out = {"name": "", "handle": "", "followers": None, "more_info": ""}
    log_debug("_parse_advertiser_meta: вход")
    i = html.find("About the advertiser")
    if i < 0:
        log_debug("_parse_advertiser_meta: секция 'About the advertiser' отсутствует — пустой результат")
        return out
    section = html[i:i + 5000]
    text = _strip(section)

    m = re.search(r"@([a-z0-9._]{3,40})", text)
    if m: out["handle"] = m.group(1)
    out["followers"] = _parse_followers(text)
    m = re.search(r"About the advertiser\s+([A-Za-z0-9][A-Za-z0-9 .\-_]{1,60}?)\s+@", text)
    if m: out["name"] = m.group(1).strip()
    m = re.search(r"More info\s+(.{20,500}?)\s+(?:Advertiser and payer|About ads|$)", text)
    if m: out["more_info"] = m.group(1).strip()
    log_debug(f"_parse_advertiser_meta: name={out['name']!r}, @{out['handle']}, "
              f"followers={out['followers']}, info_len={len(out['more_info'])}")
    return out


# ─── Advertiser & payer (юр.лицо плательщика) ───────────────────────────────

def _parse_advertiser_and_payer(html: str) -> dict:
    """Секция 'Advertiser and payer' — реальный плательщик (часто отличается от page name)."""
    out = {"current_advertiser": "", "current_payer": ""}
    log_debug("_parse_advertiser_and_payer: вход")
    i = html.find("Advertiser and payer")
    if i < 0:
        log_debug("_parse_advertiser_and_payer: секция 'Advertiser and payer' отсутствует — пустой результат")
        return out
    section = html[i:i + 4000]
    text = _strip(section)
    m = re.search(r"Current\s+Advertiser\s+([A-Za-z0-9][A-Za-z0-9 .,&\-]{2,80}?)\s+Payer", text)
    if m: out["current_advertiser"] = m.group(1).strip()
    m = re.search(r"Payer\s+([A-Za-z0-9][A-Za-z0-9 .,&\-]{2,80}?)(?:\s+About ads|$)", text)
    # Захватит payer из disclaimer тоже — берём ПОСЛЕДНЕЕ совпадение в этой секции
    matches = re.findall(r"Payer\s+([A-Za-z0-9][A-Za-z0-9 .,&\-]{2,80}?)(?:\s+About ads|$)", text)
    if matches: out["current_payer"] = matches[-1].strip()
    log_debug(f"_parse_advertiser_and_payer: {out}")
    return out


# ─── Additional assets (lead-form structure) ────────────────────────────────

def _parse_additional_assets(html: str) -> dict:
    out = {"links": [], "text_items": []}
    log_debug("_parse_additional_assets: вход")
    i = html.find("Additional assets from this ad")
    if i < 0:
        log_debug("_parse_additional_assets: секция 'Additional assets from this ad' отсутствует — пустой результат")
        return out
    section = html[i:i + 15000]

    # Links: https-URL внутри секции (исключая FB-обёртки)
    for url in re.findall(r"https?://[^\s\"'<>]+", section):
        u = url.replace("&amp;", "&").rstrip(".,)")
        if any(skip in u for skip in ["static.xx.fbcdn", "scontent.", "l.facebook.com/l.php"]):
            continue
        if u not in out["links"]:
            out["links"].append(u)

    # Text items
    j = section.find("Text")
    if j > 0:
        text_block = section[j:j + 8000]
        items = re.findall(r"<(?:li|span)[^>]*>([^<]{3,300})</(?:li|span)>", text_block)
        for raw in items:
            t = _strip(raw)
            if (t and len(t) > 2 and t not in out["text_items"]
                    and not t.startswith("http")):
                out["text_items"].append(t)
        out["text_items"] = out["text_items"][:30]
    log_debug(f"_parse_additional_assets: links={len(out['links'])}, text_items={len(out['text_items'])}")
    return out


# ─── GraphQL ad_details (network_request, не DOM) ───────────────────────────

def _flatten_breakdown(block: dict) -> list:
    """age_country_gender_reach_breakdown → плоские строки
    {country, age_range, male, female, unknown}."""
    rows = []
    for c in (block or {}).get("age_country_gender_reach_breakdown") or []:
        for b in c.get("age_gender_breakdowns") or []:
            rows.append({
                "country":   c.get("country"),
                "age_range": b.get("age_range"),
                "male":      b.get("male") or 0,
                "female":    b.get("female") or 0,
                "unknown":   b.get("unknown") or 0,
            })
    return rows


def parse_graphql_ad_details(payload: dict) -> dict:
    """Парсит GraphQL-ответ модалки (ad_details) в плоский dict.
    Источник каждого поля: network_request (не DOM-регекс).
    Несёт то, чего в DOM-таблице нет/не видно: точный EU/UK reach, ПОЛНАЯ
    демография (все страны × возрасты × м/ж/неизв.), payer + beneficiary,
    исключённые страны таргета, точные лайки/IG страницы.
    Tested: 2026-07-17 on client-a.example top-30 — 30/30 payload'ов распарсились."""
    det = (((payload or {}).get("data") or {}).get("ad_library_main") or {}).get("ad_details") or {}
    tbl = det.get("transparency_by_location") or {}
    eu = tbl.get("eu_transparency") or {}
    uk = tbl.get("uk_transparency") or {}
    br = tbl.get("br_transparency") or {}
    aaa = det.get("aaa_info") or {}
    pb = (aaa.get("payer_beneficiary_data") or [{}])[0]
    page_info = (((det.get("advertiser") or {}).get("ad_library_page_info") or {})
                 .get("page_info") or {})
    age = eu.get("age_audience") or {}

    out = {
        "source": "network_request",
        "eu_total_reach":    eu.get("eu_total_reach"),
        "uk_total_reach":    uk.get("total_reach"),
        "br_total_reach":    br.get("total_reach"),
        "eu_demographics":   _flatten_breakdown(eu),
        "uk_demographics":   _flatten_breakdown(uk),
        "gender_audience":   eu.get("gender_audience"),
        "age_audience_min":  age.get("min"),
        "age_audience_max":  age.get("max"),
        "location_audience": [
            {"name": l.get("name"), "type": l.get("type"),
             "excluded": bool(l.get("excluded"))}
            for l in (eu.get("location_audience") or [])
        ],
        "payer":             pb.get("payer"),
        "beneficiary":       pb.get("beneficiary"),
        "targets_eu":        aaa.get("targets_eu"),
        "is_ad_taken_down":  aaa.get("is_ad_taken_down"),
        "violation_types":   det.get("violation_types") or [],
        "page_name":         page_info.get("page_name"),
        "page_category":     page_info.get("page_category"),
        "page_likes":        page_info.get("likes"),
        "page_verification": page_info.get("page_verification"),
        "ig_username":       page_info.get("ig_username"),
        "ig_followers":      page_info.get("ig_followers"),
    }
    log_debug(f"parse_graphql_ad_details: eu_reach={out['eu_total_reach']} "
              f"demo_rows={len(out['eu_demographics'])} beneficiary={out['beneficiary']!r}")
    return out


# ─── Главная функция ────────────────────────────────────────────────────────

def parse_modal(html: str) -> dict:
    """Полный парсинг модалки. Возвращает dict со всеми 5 секциями.
    Секции которых нет в DOM — соответствующие поля будут пустые/None."""
    log_debug(f"parse_modal: вход, html_len={len(html)}")
    return {
        "sections_present":   detect_sections(html),
        "meta":               _parse_meta(html),
        "transparency":       _parse_transparency(html),
        "disclaimer":         _parse_disclaimer(html),
        "advertiser":         _parse_advertiser_meta(html),
        "advertiser_payer":   _parse_advertiser_and_payer(html),
        "additional_assets":  _parse_additional_assets(html),
    }


# ─── Smoke-тесты ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys, json
    from utils import setup_console
    setup_console()

    test_files = [
        ("Idgarages-Pro FR (5 секций)",
         "scans/_explore/02_modal_expanded.html"),
        ("Aerosus IT (3 секции)",
         "scans/_explore/aerosus_modal.html"),
    ]

    for label, path in test_files:
        try:
            html = open(path, encoding="utf-8").read()
        except FileNotFoundError:
            log_warn(f"{label}: файл не найден ({path})")
            continue

        print()
        log_header(f"TEST: {label}")
        d = parse_modal(html)

        print(f"sections_present ({len(d['sections_present'])}/5):")
        for s in d["sections_present"]:
            print(f"  ✓ {s}")
        print()
        m = d["meta"]
        print(f"meta:    library_id={m['library_id']}, started={m['started_running']}, "
              f"versions={m['multiple_versions']}, body={m['body'][:60]!r}")
        t = d["transparency"]
        print(f"transp:  reach={t['total_reach']}, demos={len(t['demographics'])}, "
              f"countries={t['country_targets']}, age={t['age_range']!r}, gender={t['gender_target']!r}")
        print(f"disclaim:{d['disclaimer']}")
        a = d["advertiser"]
        print(f"advert:  name={a['name']!r}, @{a['handle']}, followers={a['followers']}, "
              f"info={a['more_info'][:60]!r}")
        print(f"payer:   {d['advertiser_payer']}")
        aa = d["additional_assets"]
        print(f"assets:  links={len(aa['links'])}, text_items={len(aa['text_items'])}")
