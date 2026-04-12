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

PLATFORM_PATTERNS = {

    "facebook": {
        "patterns": [
            # Стандартный vanity: facebook.com/PageName
            r'href=["\']https?://(?:www\.)?facebook\.com/([a-zA-Z0-9][a-zA-Z0-9._-]{2,}/?)["\']',
            # /people/Name/ID/
            r'href=["\']https?://(?:www\.)?facebook\.com/people/([^/"\']+/\d+/?)["\']',
            # /profile.php?id=ID
            r'href=["\']https?://(?:www\.)?facebook\.com/profile\.php\?id=(\d+)["\']',
            # /pages/Name/ID
            r'href=["\']https?://(?:www\.)?facebook\.com/pages/([^/"\']+/\d+/?)["\']',
        ],
        # Части пути которые не являются бизнес-страницами
        "skip_paths": {
            "sharer", "share", "tr", "dialog", "photo", "video",
            "events", "groups", "pages", "help", "privacy", "legal",
            "ads", "business", "policies", "about", "login", "watch",
            "marketplace", "gaming", "fundraisers", "messenger",
            "hashtag", "notes", "reel", "story",
        },
        "base_url": "https://www.facebook.com/",
    },

    "instagram": {
        "patterns": [
            r'href=["\']https?://(?:www\.)?instagram\.com/(@?[a-zA-Z0-9._]{2,}/?)["\']',
        ],
        "skip_paths": {"p", "explore", "accounts", "legal", "about", "press"},
        "base_url": "https://www.instagram.com/",
    },

    "linkedin": {
        "patterns": [
            # Компания — с возможным /about/ суффиксом
            r'href=["\']https?://(?:www\.)?linkedin\.com/company/([a-zA-Z0-9._-]+)(?:/[^"\']*)?["\']',
            # Персональный профиль
            r'href=["\']https?://(?:www\.)?linkedin\.com/in/([a-zA-Z0-9._-]+)(?:/[^"\']*)?["\']',
        ],
        "skip_paths": {"legal", "help", "jobs", "learning", "pulse"},
        "base_url": "https://www.linkedin.com/",
        "prefer_type": "company",
    },

    "tiktok": {
        "patterns": [
            r'href=["\']https?://(?:www\.)?tiktok\.com/(@[a-zA-Z0-9._]{2,}/?)["\']',
        ],
        "skip_paths": {"legal", "about", "business", "music", "discover"},
        "base_url": "https://www.tiktok.com/",
    },

    "youtube": {
        "patterns": [
            # /channel/ID
            r'href=["\']https?://(?:www\.)?youtube\.com/(channel/[a-zA-Z0-9_-]+/?)["\']',
            # /@handle
            r'href=["\']https?://(?:www\.)?youtube\.com/(@[a-zA-Z0-9._-]+/?)["\']',
            # /c/Name (старый формат)
            r'href=["\']https?://(?:www\.)?youtube\.com/(c/[a-zA-Z0-9._-]+/?)["\']',
            # /user/Name (очень старый)
            r'href=["\']https?://(?:www\.)?youtube\.com/(user/[a-zA-Z0-9._-]+/?)["\']',
        ],
        "skip_paths": {"watch", "playlist", "results", "feed", "legal", "about"},
        "base_url": "https://www.youtube.com/",
    },

    "twitter": {
        "patterns": [
            r'href=["\']https?://(?:www\.)?(?:twitter|x)\.com/([a-zA-Z0-9_]{2,}/?)["\']',
        ],
        "skip_paths": {
            "intent", "share", "hashtag", "search", "i",
            "home", "explore", "notifications", "messages", "settings",
        },
        "base_url": "https://twitter.com/",
    },

    "pinterest": {
        "patterns": [
            r'href=["\']https?://(?:www\.)?pinterest\.com/([a-zA-Z0-9._-]+/?)["\']',
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
