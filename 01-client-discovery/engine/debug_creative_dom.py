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
from log import log_info, log_warn, log_success, log_debug, log_header

setup_console()

DEBUG_DIR = SCANS_DIR / "_debug"


async def inspect(url: str, browser):
    log_debug(f"inspect: старт для {url}")
    m = re.search(r"creative/(CR\d+)", url)
    cr_id = m.group(1) if m else "unknown"
    log_debug(f"inspect: creative_id={cr_id}")
    log_header(cr_id)
    log_info(f"  {url}")

    ctx = await browser.new_context(viewport={"width": 1400, "height": 900})
    page = await ctx.new_page()
    log_debug("inspect: контекст и страница созданы, иду на goto")
    try:
        await page.goto(url, wait_until="networkidle", timeout=30_000)
        log_debug("inspect: goto завершён (networkidle)")
    except Exception as e:
        log_warn(f"  ! goto: {e}")

    # Let hydration settle
    log_debug("inspect: жду 3000ms на гидрацию")
    await page.wait_for_timeout(3000)

    # Frame inventory
    frames = page.frames
    log_info(f"  frames: {len(frames)}")
    for i, fr in enumerate(frames):
        try:
            html = await fr.content()
            log_debug(f"inspect: frame[{i}] content получен, len={len(html)}")
            log_info(f"    [{i}] url={fr.url[:120]}  html_len={len(html)}")
        except Exception as e:
            log_warn(f"    [{i}] url={fr.url[:120]}  ERR={e}")

    # data-p attribute count anywhere on the page
    log_debug("inspect: считаю data-p на main page")
    data_p = await page.evaluate(
        "() => Array.from(document.querySelectorAll('[data-p]')).length"
    )
    log_info(f"  data-p count (main page): {data_p}")

    # variations marker
    log_debug("inspect: ищу variations marker")
    var_text = await page.evaluate(
        "() => { const el = document.body.innerText.match(/(\\d+)\\s+of\\s+(\\d+)\\s+variations/i); return el ? el[0] : null; }"
    )
    log_info(f"  variations marker: {var_text}")

    # Try to find creative body candidates (anything that looks like ad copy)
    log_debug("inspect: собираю ad-text candidates")
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
    log_info(f"  ad-text candidates ({len(candidates)}):")
    for c in candidates:
        log_info(f"    - {c[:120]}")

    # Save artifacts
    log_debug(f"inspect: сохраняю артефакты в {DEBUG_DIR}")
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    main_html = await page.content()
    (DEBUG_DIR / f"{cr_id}_main.html").write_text(main_html, encoding="utf-8")
    log_debug(f"inspect: main HTML записан ({len(main_html)} байт)")
    for i, fr in enumerate(frames):
        try:
            fhtml = await fr.content()
            (DEBUG_DIR / f"{cr_id}_frame_{i}.html").write_text(fhtml, encoding="utf-8")
            log_debug(f"inspect: frame[{i}] HTML записан ({len(fhtml)} байт)")
        except Exception as e:
            log_debug(f"inspect: frame[{i}] content недоступен, пропускаю: {e}")
    log_success(f"  saved → {DEBUG_DIR}/{cr_id}_*.html", emoji="💾")

    log_debug("inspect: закрываю контекст")
    await ctx.close()


async def main():
    log_debug("main: старт")
    urls = sys.argv[1:]
    if not urls:
        # Default: 1 empty + 1 nonempty for diff
        log_debug("main: URL не переданы — использую дефолтный набор")
        urls = [
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR00922399692023660545?region=FR",
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR00339999927662804993?region=FR",
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR01203605425824464897?region=FR",
            "https://adstransparency.google.com/advertiser/AR11291011555627368449/creative/CR00429861750280552449?region=FR",
        ]
    log_debug(f"main: обрабатываю {len(urls)} URL")

    async with async_playwright() as p:
        log_debug("main: запускаю headless Chromium")
        browser = await p.chromium.launch(headless=True)
        for url in urls:
            await inspect(url, browser)
        log_debug("main: закрываю браузер")
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
