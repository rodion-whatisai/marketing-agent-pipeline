"""One-shot DOM inspector for Google TC creative pages.

Opens a creative URL, waits for hydration, dumps:
  - frame URLs and their content lengths
  - data-p attributes count
  - variations marker text ('1 of N variations')
  - main page HTML to scans/_debug/<creative_id>.html
  - frame HTMLs to scans/_debug/<creative_id>_frame_<N>.html

Usage:
    python debug_creative_dom.py <ad_link> [<ad_link> ...]
"""
import asyncio
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

from utils import SCANS_DIR, setup_console

setup_console()

DEBUG_DIR = SCANS_DIR / "_debug"


async def inspect(url: str, browser):
    m = re.search(r"creative/(CR\d+)", url)
    cr_id = m.group(1) if m else "unknown"
    print(f"\n=== {cr_id} ===")
    print(f"  {url}")

    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
    except Exception as e:
        print(f"  ! goto: {e}")

    # Let hydration settle
    await page.wait_for_timeout(3000)

    # Frame inventory
    frames = page.frames
    print(f"  frames: {len(frames)}")
    for i, fr in enumerate(frames):
        try:
            html = await fr.content()
            print(f"    [{i}] url={fr.url[:120]}  html_len={len(html)}")
        except Exception as e:
            print(f"    [{i}] url={fr.url[:120]}  ERR={e}")

    # data-p attribute count anywhere on the page
    data_p = await page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-p]')).length"
    )
    print(f"  data-p count (main page): {data_p}")

    # variations marker
    var_text = await page.evaluate(
        "() => { const el = document.body.innerText.match(/(\\d+)\\s+of\\s+(\\d+)\\s+variations/i); return el ? el[0] : null; }"
    )
    print(f"  variations marker: {var_text}")

    # Try to find creative body candidates (anything that looks like ad copy)
    candidates = await page.evaluate(
        """() => {
        const out = [];
        const all = document.querySelectorAll('div, span, h1, h2, h3, p, a');
        all.forEach(el => {
            const t = (el.innerText || '').trim();
            if (t.length >= 8 && t.length <= 200 && /[a-zA-Zà-üÀ-Ü]/.test(t)) {
                // Skip common UI labels
                const isShortLabel = t.length < 30 && /^(Ad funded by|Topic|Format|First shown|Last shown|Times shown|Audience|Report this ad|FAQ|Home|Ad details|Shown in)/i.test(t);
                if (!isShortLabel) out.push(t);
            }
        });
        // Dedupe + first 30
        return [...new Set(out)].slice(0, 30);
    }"""
    )
    print(f"  ad-text candidates ({len(candidates)}):")
    for c in candidates:
        print(f"    - {c[:120]}")

    # Save artifacts
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    main_html = await page.content()
    (DEBUG_DIR / f"{cr_id}_main.html").write_text(main_html, encoding="utf-8")
    for i, fr in enumerate(frames):
        try:
            fhtml = await fr.content()
            (DEBUG_DIR / f"{cr_id}_frame_{i}.html").write_text(fhtml, encoding="utf-8")
        except Exception:
            pass
    print(f"  saved → {DEBUG_DIR}/{cr_id}_*.html")

    await ctx.close()


async def main():
    urls = sys.argv[1:]
    if not urls:
        # Default: 1 empty + 1 nonempty for diff
        urls = [
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR00922399692023660545?region=FR",
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR00339999927662804993?region=FR",
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR01203605425824464897?region=FR",
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR00429861750280552449?region=FR",
        ]

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        for url in urls:
            await inspect(url, browser)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
