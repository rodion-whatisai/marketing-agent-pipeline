"""
TNC Pipeline — Shared Constants
================================
Метки, иконки, приоритеты. Импортируй отсюда — не копируй.
"""

TYPE_LABELS = {
    "lead_form":       "🔴 Lead Forms",
    "booking_confirm": "🔴 Booking / Confirm",
    "quote":           "🔴 Quote",
    "checkout":        "🔴 Checkout",
    "homepage":        "🟠 Homepage",
    "location":        "🟠 Location Pages",
    "product":         "🟠 Product / Listing Pages",
    "use_case":        "🟠 Use Case Pages",
    "search_results":  "🟠 Search / Browse Pages",
    "pricing":         "🟠 Pricing",
    "faq_support":     "🟡 FAQ / Guides",
    "about":           "🟡 About",
    "blog_content":    "🟡 Blog",
    "legal":           "⚪ Legal",
    "technical":       "⚪ Technical",
    "general":         "⚪ General",
}

TYPE_LABELS_SHORT = {
    "lead_form":       "Lead form",
    "booking_confirm": "Booking confirm",
    "quote":           "Quote",
    "checkout":        "Checkout",
    "homepage":        "Homepage",
    "pricing":         "Pricing",
    "location":        "Location",
    "product":         "Product",
    "use_case":        "Use case",
    "search_results":  "Search results",
    "faq_support":     "FAQ / support",
    "careers":         "Careers",
    "about":           "About",
    "blog_content":    "Blog",
    "legal":           "Legal",
    "technical":       "Technical",
    "general":         "General ❓",
}

PRIORITY_LABELS = {
    1: "🔴 CRITICAL",
    2: "🟠 HIGH",
    3: "🟡 MEDIUM",
    4: "🟢 LOW",
    5: "⚪ SKIP",
}

PRIORITY_ICONS = {
    1: "🔴",
    2: "🟠",
    3: "🟡",
    4: "🟢",
    5: "⚪",
}

PLATFORM_ICONS = {
    "Meta":             "📘 Meta Pixel",
    "Google Analytics": "📊 Google Analytics",
    "Google Ads":       "🟡 Google Ads",
    "Bing/Microsoft":   "🔷 Bing/Microsoft",
    "LinkedIn":         "💼 LinkedIn",
    "TikTok":           "🎵 TikTok",
    "Snapchat":         "👻 Snapchat",
    "Pinterest":        "📌 Pinterest",
}

# Порядок отображения платформ — Meta всегда первая
PLATFORM_PRIORITY = [
    "Meta", "Google Analytics", "Google Ads",
    "TikTok", "Bing/Microsoft", "LinkedIn",
    "Snapchat", "Pinterest",
]
