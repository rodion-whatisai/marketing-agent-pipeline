"""
Scanner router — возвращает нужный scanner по платформе.
"""

from . import shopify_scanner, wordpress_scanner, generic_scanner


def get_scanner(platform: str):
    """Returns the scan_page function for the given platform."""
    platform = (platform or "").lower()
    if platform == "shopify":
        return shopify_scanner.scan_page
    if platform == "wordpress":
        return wordpress_scanner.scan_page
    # Webflow, Squarespace, Wix, custom — generic
    return generic_scanner.scan_page
