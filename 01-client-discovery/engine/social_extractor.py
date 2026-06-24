"""
TNC Social Extractor
====================
Чистый парсер ссылок на соцсети из HTML.
Каждая платформа — отдельный паттерн, только из href атрибутов.
Не путает handle одной платформы с URL другой.

Использование:
    from social_extractor import extract_socials
    socials = extract_socials(html, base_url)
"""

import re
from urllib.parse import urlparse

# ─── Паттерны для каждой платформы ───────────────────────────────────────────
# Ищем только в href="..." — не по всему тексту
# Каждый паттерн возвращает полный URL

# ─── Общая идея ──────────────────────────────────────────────────────────────
# Каждый паттерн ловит URL платформы ВЕЗДЕ в HTML (не только в href).
# Не требует закрывающую кавычку → пропускает query (?fbclid=...), hash (#...),
# подстраницы (/posts/123). Граница пути — lookahead на [/?#"' пробел < > или конец].
# Поддерживаем mobile/web/locale-субдомены (m.facebook.com, fr-fr.facebook.com).
# Допускаем protocol-relative URL (//www.facebook.com/...).
# Vanity-handle всегда матчится первым; multi-segment (pages/, people/) и
# numeric (profile.php?id=) — отдельными паттернами с приоритетом через skip_paths.

# Префикс субдомена FB — locale (xx-xx), www, m, web
_FB_SUB = r'(?:[a-z]{2}-[a-z]{2}\.|www\.|m\.|web\.)?'
# Граница после handle: следующий слэш / query / hash / кавычка / пробел / угол / конец
_BOUNDARY = r'(?=[/?#"\'\s<>]|$)'

PLATFORM_PATTERNS = {

    "facebook": {
        "patterns": [
            # Vanity: facebook.com/PageName (с любыми query/hash/субпутями после)
            rf'(?:https?:)?//{_FB_SUB}facebook\.com/([a-zA-Z0-9][a-zA-Z0-9._-]{{2,}}){_BOUNDARY}',
            # /people/Name/ID
            rf'(?:https?:)?//{_FB_SUB}facebook\.com/people/([a-zA-Z0-9._-]+/\d+){_BOUNDARY}',
            # /profile.php?id=ID
            rf'(?:https?:)?//{_FB_SUB}facebook\.com/profile\.php\?id=(\d+)',
            # /pages/Name/ID
            rf'(?:https?:)?//{_FB_SUB}facebook\.com/pages/([a-zA-Z0-9._-]+/\d+){_BOUNDARY}',
        ],
        # Системные пути, не являющиеся бизнес-страницами
        "skip_paths": {
            "sharer", "share", "tr", "dialog", "photo", "video",
            "events", "groups", "pages", "help", "privacy", "legal",
            "ads", "business", "policies", "about", "login", "watch",
            "marketplace", "gaming", "fundraisers", "messenger",
            "hashtag", "notes", "reel", "story", "people",
            "profile.php", "plugins", "v2.0", "tr.php",
        },
        "base_url": "https://www.facebook.com/",
    },

    "instagram": {
        "patterns": [
            rf'(?:https?:)?//(?:www\.)?instagram\.com/(@?[a-zA-Z0-9._]{{2,}}){_BOUNDARY}',
        ],
        "skip_paths": {"p", "explore", "accounts", "legal", "about", "press", "reel", "stories"},
        "base_url": "https://www.instagram.com/",
    },

    "linkedin": {
        "patterns": [
            # Компания
            rf'(?:https?:)?//(?:[a-z]{{2}}\.)?(?:www\.)?linkedin\.com/company/([a-zA-Z0-9._-]+){_BOUNDARY}',
            # Персональный профиль
            rf'(?:https?:)?//(?:[a-z]{{2}}\.)?(?:www\.)?linkedin\.com/in/([a-zA-Z0-9._-]+){_BOUNDARY}',
        ],
        "skip_paths": {"legal", "help", "jobs", "learning", "pulse"},
        "base_url": "https://www.linkedin.com/",
        "prefer_type": "company",
    },

    "tiktok": {
        "patterns": [
            rf'(?:https?:)?//(?:www\.)?tiktok\.com/(@[a-zA-Z0-9._]{{2,}}){_BOUNDARY}',
        ],
        "skip_paths": {"legal", "about", "business", "music", "discover"},
        "base_url": "https://www.tiktok.com/",
    },

    "youtube": {
        "patterns": [
            # /channel/ID
            rf'(?:https?:)?//(?:www\.)?youtube\.com/(channel/[a-zA-Z0-9_-]+){_BOUNDARY}',
            # /@handle
            rf'(?:https?:)?//(?:www\.)?youtube\.com/(@[a-zA-Z0-9._-]+){_BOUNDARY}',
            # /c/Name
            rf'(?:https?:)?//(?:www\.)?youtube\.com/(c/[a-zA-Z0-9._-]+){_BOUNDARY}',
            # /user/Name
            rf'(?:https?:)?//(?:www\.)?youtube\.com/(user/[a-zA-Z0-9._-]+){_BOUNDARY}',
        ],
        "skip_paths": {"watch", "playlist", "results", "feed", "legal", "about", "shorts"},
        "base_url": "https://www.youtube.com/",
    },

    "twitter": {
        "patterns": [
            rf'(?:https?:)?//(?:www\.)?(?:twitter|x)\.com/([a-zA-Z0-9_]{{2,}}){_BOUNDARY}',
        ],
        "skip_paths": {
            "intent", "share", "hashtag", "search", "i",
            "home", "explore", "notifications", "messages", "settings",
        },
        "base_url": "https://twitter.com/",
    },

    "pinterest": {
        "patterns": [
            rf'(?:https?:)?//(?:[a-z]{{2}}\.)?pinterest\.com/([a-zA-Z0-9._-]+){_BOUNDARY}',
        ],
        "skip_paths": {"pin", "search", "explore", "news", "ideas"},
        "base_url": "https://www.pinterest.com/",
    },
}


def extract_socials(html: str, base_domain: str = "") -> dict:
    """
    Извлекает ссылки на соцсети из HTML.
    Возвращает dict: {platform: {"url": ..., "handle": ..., "type": ...}}
    """
    result = {}

    for platform, config in PLATFORM_PATTERNS.items():
        found_company = []
        found_personal = []
        found_other = []

        for i, pattern in enumerate(config["patterns"]):
            matches = re.findall(pattern, html, re.IGNORECASE)
            for match in matches:
                path = match.strip("/").split("?")[0].split("#")[0]
                first_segment = path.split("/")[0].lower()

                if first_segment in config.get("skip_paths", set()):
                    continue

                if platform == "instagram":
                    path = path.lstrip("@")

                base = config["base_url"]

                # LinkedIn — добавляем prefix обратно
                if platform == "linkedin":
                    if i == 0:  # company pattern
                        full_url = f"https://www.linkedin.com/company/{path.rstrip('/')}/"
                        link_type = "company"
                        handle = path.strip("/")
                    else:  # personal pattern
                        full_url = f"https://www.linkedin.com/in/{path.rstrip('/')}/"
                        link_type = "personal"
                        handle = path.strip("/")
                else:
                    full_url = base + path.rstrip("/") + "/"
                    handle = path.rstrip("/").split("/")[-1]
                    link_type = "profile"

                if platform == "facebook":
                    if "/people/" in path or path.startswith("people/"):
                        link_type = "people"
                    elif "/pages/" in path or path.startswith("pages/"):
                        link_type = "pages"

                entry = {
                    "url": full_url,
                    "handle": handle,
                    "type": link_type,
                    "platform": platform,
                }

                urls_so_far = [f["url"] for f in found_company + found_personal + found_other]
                if full_url not in urls_so_far:
                    if link_type == "company":
                        found_company.append(entry)
                    elif link_type == "personal":
                        found_personal.append(entry)
                    else:
                        found_other.append(entry)

        # Для LinkedIn — company имеет приоритет
        if platform == "linkedin":
            found = found_company if found_company else found_personal
        else:
            found = found_company + found_personal + found_other

        if found:
            # Приоритизируем по близости к домену бренда
            best = _pick_best(found, base_domain, platform)
            result[platform] = best
            if len(found) > 1:
                result[f"{platform}_all"] = found

    return result


def _brand_name(domain: str) -> str:
    """Вытаскивает имя бренда из домена: alumiermd.com → alumiermd"""
    if not domain:
        return ""
    d = domain.lower().split(":")[0]  # убираем порт если есть
    # Убираем поддомен типа us. ca. www.
    parts = d.split(".")
    # Берём основной домен без TLD
    if len(parts) >= 2:
        return parts[-2]
    return parts[0]


def _score_handle(handle: str, brand: str) -> int:
    """Чем меньше score — тем лучше совпадение с брендом."""
    if not brand:
        return 99
    h = handle.lower().replace("_", "").replace("-", "").replace(".", "").replace("@", "")
    b = brand.lower().replace("_", "").replace("-", "").replace(".", "")
    if h == b:
        return 0   # точное совпадение
    if h.startswith(b) or h.endswith(b):
        return 1   # начинается или заканчивается брендом (alumiermdusa, alumiermdca)
    if b in h:
        return 2   # содержит бренд где-то
    return 10      # не связан с брендом


def _pick_best(candidates: list, base_domain: str, platform: str) -> dict:
    """Выбирает наиболее релевантный аккаунт по близости к домену бренда."""
    brand = _brand_name(base_domain)
    if not brand or len(candidates) == 1:
        return candidates[0]

    scored = [(c, _score_handle(c["handle"], brand)) for c in candidates]
    scored.sort(key=lambda x: x[1])
    best, best_score = scored[0]

    # Если лучший кандидат не связан с брендом — помечаем как uncertain
    if best_score >= 10:
        best = dict(best)
        best["uncertain"] = True
        best["uncertain_reason"] = f"no handle matches brand '{brand}'"

    return best


def get_social_display(socials: dict) -> list:
    """Возвращает список строк для вывода в консоль."""
    lines = []
    platforms = ["facebook", "instagram", "linkedin", "tiktok",
                 "youtube", "twitter", "pinterest"]

    for platform in platforms:
        if platform in socials:
            item = socials[platform]
            url = item["url"]
            handle = item["handle"]
            ptype = item.get("type", "")
            type_str = f" [{ptype}]" if ptype and ptype != "profile" else ""
            lines.append((platform, url, handle, type_str))

    return lines


if __name__ == "__main__":
    import sys
    import requests

    url = sys.argv[1] if len(sys.argv) > 1 else "https://bandago.com"
    HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120.0.0.0"}

    r = requests.get(url, headers=HEADERS, timeout=10)
    domain = urlparse(url).netloc

    socials = extract_socials(r.text, domain)

    print(f"\nСоцсети на {url}:")
    for platform, item in socials.items():
        if not platform.endswith("_all"):
            print(f"  {platform:<12} {item['url']}")
