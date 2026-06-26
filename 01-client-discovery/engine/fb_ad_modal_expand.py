"""
Module 4: FB Ad Modal Expander
===============================
ВХОД:  page с УЖЕ открытой модалкой (после Step 3)
ВЫХОД: diag dict — для каждой ПРИСУТСТВУЮЩЕЙ секции статус раскрытия

Hybrid стратегия:
    A) Playwright row click — работает для правых аккордеонов
       (Transparency / Disclaimer / About advertiser / Advertiser & payer)
    B) JS-dispatched MouseEvent на <a href="#"> — нужен для левого
       "Additional assets from this ad" (он в портале левой колонки)

Сначала детектим какие секции вообще присутствуют (через Step 5),
и пробуем раскрыть ТОЛЬКО их. Это убирает шум 'FAILED Δ0' для отсутствующих.

Standalone:
    python fb_ad_modal_expand.py 1843412783120928 [--save-html]
"""
import sys
import argparse
from pathlib import Path

from utils import setup_console
setup_console()

from log import log_info, log_warn, log_error, log_debug, log_success, log_step, log_header

# Импорты соседних шагов
from fb_ad_modal_parse import detect_sections, ALL_SECTION_LABELS


# ─── Helpers ────────────────────────────────────────────────────────────────

def _scroll_heading_into_view(page, label: str):
    """JS-скролл heading'а в видимую область (работает в портале/нестед-скролле)."""
    log_debug(f"_scroll_heading_into_view: скроллю heading '{label}'")
    page.evaluate("""
    (label) => {
      const headings = Array.from(document.querySelectorAll('[role="heading"], h2, h3, h4'));
      const h = headings.find(x => x.textContent.trim() === label);
      if (h) h.scrollIntoView({block: 'center', behavior: 'instant'});
    }
    """, label)


def _try_row_click(page, label: str, before_size: int) -> int:
    """Strategy A — Playwright row click. Возвращает Δ bytes (или 0 если не сработал)."""
    log_debug(f"_try_row_click: пробую row click по '{label}' (before_size={before_size})")
    try:
        row = page.locator(
            f"div:has(> div > [role='heading']:text-is('{label}'))"
        ).first
        row.click(force=True, timeout=2500)
    except Exception as e:
        log_debug(f"_try_row_click: row click по '{label}' не сработал: {e}")
        return 0
    page.wait_for_timeout(1200)
    delta = len(page.content()) - before_size
    log_debug(f"_try_row_click: '{label}' Δ={delta}")
    return delta


def _try_js_dispatch(page, label: str, before_size: int) -> int:
    """Strategy B — JS dispatch на <a href='#'> в строке heading'а. Δ bytes."""
    log_debug(f"_try_js_dispatch: пробую JS dispatch по '{label}' (before_size={before_size})")
    page.evaluate("""
    (label) => {
      const headings = Array.from(document.querySelectorAll('[role="heading"]'));
      const h = headings.find(x => x.textContent.trim() === label);
      if (!h) return;
      let row = h.parentElement;
      for (let d = 0; d < 6 && row; d++) {
          const a = row.querySelector('a[href="#"]');
          if (a) {
              ['pointerdown','pointerup','click'].forEach(t => {
                a.dispatchEvent(new MouseEvent(t, {bubbles:true,cancelable:true,view:window,buttons:1}));
              });
              return;
          }
          row = row.parentElement;
      }
    }
    """, label)
    page.wait_for_timeout(1500)
    delta = len(page.content()) - before_size
    log_debug(f"_try_js_dispatch: '{label}' Δ={delta}")
    return delta


def _wait_for_transparency_loaded(page, timeout_ms: int = 6000) -> bool:
    """Async ожидание: появилась ли реальная цифра Reach в DOM."""
    import time
    log_debug(f"_wait_for_transparency_loaded: жду Reach в DOM (timeout={timeout_ms}ms)")
    start = time.time()
    while (time.time() - start) * 1000 < timeout_ms:
        try:
            ok = page.evaluate("""
            () => {
              const dialogs = document.querySelectorAll('[role="dialog"]');
              for (const d of dialogs) {
                const txt = d.textContent;
                if (txt.includes('Reach by location') || /Reach\\s+[\\d,]+/.test(txt)) return true;
              }
              return false;
            }
            """)
            if ok:
                log_debug("_wait_for_transparency_loaded: Reach найден в DOM")
                return True
        except Exception as e:
            log_debug(f"_wait_for_transparency_loaded: evaluate упал, ретрай: {e}")
        page.wait_for_timeout(500)
    log_debug("_wait_for_transparency_loaded: timeout — Reach не появился")
    return False


# ─── Главная функция ────────────────────────────────────────────────────────

def expand_all_present_accordions(page, sections_to_expand=None,
                                    verbose: bool = True) -> dict:
    """
    Раскрывает только те секции, которые присутствуют в DOM.
    sections_to_expand: list[str] — если None, автодетект через detect_sections().
    Возвращает {sections_present, diag: {label: status_str}, html}.
    """
    log_debug(f"expand_all_present_accordions: вход (sections_to_expand={sections_to_expand}, verbose={verbose})")
    html_initial = page.content()
    if sections_to_expand is None:
        log_debug("expand_all_present_accordions: автодетект секций через detect_sections()")
        sections_to_expand = detect_sections(html_initial)

    if verbose:
        log_info(f"📋 секций в DOM: {len(sections_to_expand)}/5")
        for s in sections_to_expand:
            print(f"         · {s}")

    diag = {}
    for label in sections_to_expand:
        log_debug(f"expand_all_present_accordions: обрабатываю секцию '{label}'")
        if verbose: log_step(f"expand: '{label}'...", emoji="🔧")

        _scroll_heading_into_view(page, label)
        page.wait_for_timeout(300)
        before = len(page.content())

        # Strategy A: row click
        delta = _try_row_click(page, label, before)
        if delta >= 500:
            diag[label] = f"row_click +{delta:,}"
            log_debug(f"expand_all_present_accordions: '{label}' раскрыт через row_click (Δ{delta})")
            if verbose: log_success(f"row_click  +{delta:,}", emoji="✓")
            continue

        # Strategy B: JS dispatch (нужен для 'Additional assets from this ad')
        delta = _try_js_dispatch(page, label, before)
        if delta >= 500:
            diag[label] = f"js_dispatch +{delta:,}"
            log_debug(f"expand_all_present_accordions: '{label}' раскрыт через js_dispatch (Δ{delta})")
            if verbose: log_success(f"js_dispatch +{delta:,}", emoji="✓")
        else:
            diag[label] = f"NO_GROWTH Δ{delta}"
            log_debug(f"expand_all_present_accordions: '{label}' не вырос ни одной стратегией (Δ{delta})")
            if verbose: log_warn(f"no_growth  Δ{delta}")

    # Финальное ожидание async-данных Transparency
    log_debug("expand_all_present_accordions: финальное ожидание Transparency async-данных")
    _wait_for_transparency_loaded(page, timeout_ms=6000)
    page.wait_for_timeout(700)

    return {
        "sections_present": sections_to_expand,
        "diag":             diag,
        "html":             page.content(),
    }


# ─── Standalone ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("library_id", help="library_id (цифры)")
    ap.add_argument("--save-html", action="store_true",
                    help="сохранить final HTML в scans/_explore/expanded_<id>.html")
    args = ap.parse_args()

    if not args.library_id.isdigit():
        log_error(f"Step 4 принимает только library_id (цифры), не '{args.library_id}'")
        print(f"   Для прогона от домена: python fb_scan.py <domain> (когда будет готов)")
        sys.exit(1)

    log_header(f"FB AD MODAL EXPANDER — library_id: {args.library_id}")

    # Step 3 → открываем модалку (нам нужен page после)
    from playwright.sync_api import sync_playwright
    from fb_ad_modal_open import open_ad_modal

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            locale="en-US",
            viewport={"width": 1400, "height": 900},
        )
        page = ctx.new_page()

        log_step("Step 3: открываю модалку...", emoji="🌐")
        open_result = open_ad_modal(args.library_id, page=page, verbose=True)
        if not open_result.get("success"):
            log_error(f"Step 3 не удался: {open_result.get('error')}")
            browser.close()
            sys.exit(1)

        log_step("Step 4: раскрываю аккордеоны...", emoji="🔧")
        result = expand_all_present_accordions(page)

        log_header("RESULT")
        print(f"sections_present ({len(result['sections_present'])}/5):")
        for s in ALL_SECTION_LABELS:
            mark = "✓" if s in result["sections_present"] else "✗"
            status = result["diag"].get(s, "—")
            print(f"  {mark} {s:<35} {status}")
        print(f"\nfinal HTML size: {len(result['html']):,} bytes")

        if args.save_html:
            out = Path(f"scans/_explore/expanded_{args.library_id}.html").resolve()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(result["html"], encoding="utf-8")
            log_success(f"saved: {out}", emoji="💾")

        browser.close()
