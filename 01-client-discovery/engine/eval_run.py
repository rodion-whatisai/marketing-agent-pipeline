"""
TNC Testbed — eval_run: раннер испытательного стенда
=====================================================
Прогоняет step2-скан по ЗАМОРОЖЕННОМУ step1 каждого домена корпуса, сравнивает
с эталоном (eval_lib) и печатает счёт «N из M MATCH». Регрессия сканера (FAIL)
валит прогон (exit 1); дрейф сайта (DRIFT) — жёлтая пометка, не валит.

Запуск (см. TESTBED-PLAN.md):
    python eval_run.py                    # все домены корпуса, у которых есть эталон
    python eval_run.py --fast             # только fast-домены (~10 мин) — перед коммитом
    python eval_run.py --domains a.com b.fr
    python eval_run.py --skip-scan       # не пересканировать — сравнить лежащие step2 (отладка диффера)
    python eval_run.py --history          # напечатать кривую доверия и выйти
    python eval_run.py --refresh-step1 d  # сознательно пересоздать замороженный step1 домена

Выход: scans/_eval/<ts>/scorecard.md + .csv, строка в golden/history.csv (коммитится).
"""

import sys
import csv
import json
import time
import shutil
import argparse
import datetime
from unittest.mock import patch

from utils import setup_console
setup_console()  # UTF-8 до первого вывода — cp1252-крэш на эмодзи статусов

import log
from log import log_error, log_success, log_info, log_step, log_warn, log_header
import eval_lib as ev
from make_expected import scanner_commit

EVAL_DIR = ev.ENGINE_DIR / "scans" / "_eval"
HISTORY_CSV = ev.GOLDEN_DIR / "history.csv"
HISTORY_HEADERS = ["ts", "commit", "mode", "domains", "checks", "match", "fail", "drift", "pct"]
SCORECARD_HEADERS = ["domain", "platform", "checks", "match", "fail", "drift", "pct", "duration_s", "note"]


# ─── Прогон одного домена ─────────────────────────────────────────────────────

def run_domain(domain: str, run_dir, skip_scan: bool) -> dict:
    """Скан по замороженному step1 → сравнение с эталоном.
    Возвращает {'domain', 'summary', 'checks', 'duration_s', 'note'} или {'skipped': причина}."""
    exp_path = ev.expected_path(domain)
    if not exp_path.exists():
        return {"domain": domain, "skipped": "нет эталона (make_expected ещё не прогнан)"}
    s1_path = ev.frozen_step1_path(domain)
    if not s1_path.exists():
        return {"domain": domain, "skipped": f"нет замороженного step1: {s1_path}"}

    expected = ev.load_expected(domain)
    step1 = ev.load_json(s1_path)
    step2_path = ev.ENGINE_DIR / "scans" / domain / f"{domain}_step2.json"

    t0 = time.time()
    if skip_scan:
        if not step2_path.exists():
            return {"domain": domain, "skipped": "--skip-scan, а step2.json на диске нет"}
        actual = ev.load_json(step2_path)
        note = "skip-scan: сравнение с лежащим step2"
    else:
        # архив прежнего результата — паттерн archive-not-delete (mass_run_creatives).
        # Секундная гранулярность: повторный прогон в ту же минуту не затирает архив
        if step2_path.exists():
            arch_dir = step2_path.parent / "_archive"
            arch_dir.mkdir(exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
            shutil.copy2(step2_path, arch_dir / f"{domain}_step2_pre-eval-{stamp}.json")
        import step2_scan
        log_step(f"Скан {domain} по замороженному step1", emoji="🔬")
        # click_mode=True — боевой режим (у функции дефолт False, включает CLI-обёртка)
        actual = step2_scan.run(str(s1_path), max_priority=2, click_mode=True)
        note = ""
    duration = round(time.time() - t0, 1)

    # копия результата в папку прогона — чтобы дифф был воспроизводим
    (run_dir / f"{domain}_step2.json").write_text(
        json.dumps(actual, ensure_ascii=False, indent=2), encoding="utf-8")

    res = ev.compare_site(expected, actual, step1)
    return {"domain": domain, "summary": res["summary"], "checks": res["checks"],
            "duration_s": duration, "note": note}


# ─── Scorecard ────────────────────────────────────────────────────────────────

def write_scorecard(run_dir, results: list, skipped: list, mode: str, commit: str):
    """scorecard.md + scorecard.csv + печать в консоль."""
    total = ev.summarize([c for r in results for c in r["checks"]])
    corpus = ev.load_corpus()["domains"]

    csv_path = run_dir / "scorecard.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=SCORECARD_HEADERS)
        w.writeheader()
        for r in results:
            s = r["summary"]
            w.writerow({"domain": r["domain"],
                        "platform": corpus.get(r["domain"], {}).get("platform", "?"),
                        "checks": s["total"], "match": s["match"], "fail": s["fail"],
                        "drift": s["drift"], "pct": s["pct"],
                        "duration_s": r["duration_s"], "note": r["note"]})

    lines = [f"# Eval {datetime.date.today().isoformat()} · commit {commit} · режим {mode}", ""]
    lines.append("| Домен | Платформа | Проверок | MATCH | FAIL | DRIFT | Счёт |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        s = r["summary"]
        icon = "✅" if s["fail"] == 0 else "🚨"
        lines.append(f"| {r['domain']} | {corpus.get(r['domain'], {}).get('platform', '?')} "
                     f"| {s['total']} | {s['match']} | {s['fail']} | {s['drift']} "
                     f"| {s['match']}/{s['total']} {icon} |")
    lines.append(f"| **ИТОГО** |  | {total['total']} | {total['match']} "
                 f"| {total['fail']} | {total['drift']} | **{total['pct']}%** |")
    lines.append("")

    fails = [(r["domain"], c) for r in results for c in r["checks"] if c["verdict"] == ev.FAIL]
    drifts = [(r["domain"], c) for r in results for c in r["checks"]
              if c["verdict"] in (ev.DRIFT, ev.DRIFT_NEW)]
    if fails:
        lines.append("## FAIL — регрессии сканера (валят прогон)")
        for d, c in fails:
            lines.append(f"- **{d}** {c['path']} `{c['field']}`: ожидалось {c['expected']}, "
                         f"получено {c['actual']}. {c['note']}")
        lines.append("")
    if drifts:
        lines.append("## DRIFT — похоже, сайт изменился (перепроверь и обнови эталон)")
        for d, c in drifts:
            lines.append(f"- {d} {c['path']} `{c['field']}`: {c['note']}")
        lines.append("")
    if skipped:
        lines.append("## Пропущено")
        for r in skipped:
            lines.append(f"- {r['domain']}: {r['skipped']}")
        lines.append("")

    md_path = run_dir / "scorecard.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")

    # консоль
    print()
    log_header("SCORECARD")
    for r in results:
        s = r["summary"]
        fn = log_success if s["fail"] == 0 else log_error
        fn(f"{r['domain']:28} {s['match']}/{s['total']} MATCH"
           + (f", FAIL={s['fail']}" if s["fail"] else "")
           + (f", DRIFT={s['drift']}" if s["drift"] else "")
           + f"  ({r['duration_s']}s)")
    for r in skipped:
        log_warn(f"{r['domain']:28} SKIP — {r['skipped']}")
    for d, c in fails:
        log_error(f"  FAIL {d} {c['path']} {c['field']}: {c['note'][:100]}")
    for d, c in drifts:
        log_warn(f"  DRIFT {d} {c['path']} {c['field']}: {c['note'][:100]}")
    log_info(f"Итог: {total['match']}/{total['total']} MATCH ({total['pct']}%), "
             f"FAIL={total['fail']}, DRIFT={total['drift']}")
    log_info(f"Scorecard: {md_path}")
    return total


def append_history(total: dict, mode: str, results: list, commit: str):
    # domains = РЕАЛЬНО сравненные (не запланированные — пропущенные завышали бы покрытие)
    need_header = not HISTORY_CSV.exists() or HISTORY_CSV.stat().st_size == 0
    with open(HISTORY_CSV, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_HEADERS)
        if need_header:
            w.writeheader()
        w.writerow({"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "commit": commit, "mode": mode, "domains": len(results),
                    "checks": total["total"], "match": total["match"],
                    "fail": total["fail"], "drift": total["drift"], "pct": total["pct"]})


def print_history():
    if not HISTORY_CSV.exists():
        log_warn("history.csv ещё нет — ни одного прогона не записано")
        return
    log_header("Кривая доверия — golden/history.csv")
    with open(HISTORY_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                bar = "█" * int(float(row["pct"]) // 5)
                print(f"  {row['ts']}  {row['commit']:>9}  {row['mode']:<6} "
                      f"{row['match']:>4}/{row['checks']:<4} FAIL={row['fail']:<3} "
                      f"{row['pct']:>5}% {bar}")
            except (KeyError, ValueError, TypeError):
                log_warn(f"битая строка history.csv пропущена: {row}")


# ─── refresh-step1 ────────────────────────────────────────────────────────────

def refresh_step1(domain: str):
    """Сознательное пересоздание замороженного step1 (живой sitemap изменился).
    ВНИМАНИЕ: ручное усечение to_scan (fast-домены, раздутые step1) теряется —
    после refresh усечение и перезаверку эталона делать заново."""
    log_warn(f"refresh-step1 {domain}: усечение to_scan и эталон потребуют перезаверки!")
    import step1_sitemap
    with patch("builtins.input", lambda *a, **k: "1"):   # авто-ответы как в batch_step1
        # skip_discovery=True — стенду нужен только sitemap+классификация, без FB-разведки
        step1_sitemap.run(domain, limit=9999, force_all=True, skip_discovery=True)
    src = ev.ENGINE_DIR / "scans" / domain / f"{domain}_step1.json"
    if not src.exists():
        log_error(f"step1 не создался: {src}")
        sys.exit(1)
    dst = ev.frozen_step1_path(domain)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    log_success(f"Заморожен свежий step1: {dst}", emoji="🧊")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Раннер испытательного стенда (см. TESTBED-PLAN.md)")
    parser.add_argument("--domains", nargs="*", default=None, help="только эти домены")
    parser.add_argument("--fast", action="store_true", help="только fast-домены корпуса (перед коммитом)")
    parser.add_argument("--skip-scan", action="store_true", help="сравнить лежащие step2 без пересканирования")
    parser.add_argument("--history", action="store_true", help="напечатать кривую доверия и выйти")
    parser.add_argument("--refresh-step1", default=None, metavar="DOMAIN",
                        help="сознательно пересоздать замороженный step1 домена")
    parser.add_argument("--debug", action="store_true", help="полный лог-поток (дефолт: INFO)")
    args = parser.parse_args()

    if not args.debug:
        log.set_level("INFO")

    if args.history:
        print_history()
        return 0

    corpus = ev.load_corpus()["domains"]

    if args.refresh_step1:
        if args.refresh_step1 not in corpus:
            # защита от опечатки: иначе живой скан заморозит мусорный golden/<typo>/
            log_error(f"{args.refresh_step1!r} нет в корпусе (см. golden/corpus.json)")
            return 1
        refresh_step1(args.refresh_step1)
        return 0

    domains = list(corpus.keys())
    mode = "full"
    if args.fast:
        domains = [d for d, meta in corpus.items() if "fast" in meta.get("tags", [])]
        mode = "fast"
    if args.domains is not None:      # именно is not None: голый --domains = ошибка, не full-прогон
        if not args.domains:
            log_error("--domains задан без доменов — перечисли их или убери флаг")
            return 1
        unknown = [d for d in args.domains if d not in corpus]
        if unknown:
            log_error(f"Не в корпусе: {', '.join(unknown)} (см. golden/corpus.json)")
            return 1
        domains = list(dict.fromkeys(args.domains))   # дедуп с сохранением порядка
        mode = "custom"

    run_ts = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = EVAL_DIR / run_ts
    run_dir.mkdir(parents=True, exist_ok=True)
    commit = scanner_commit()

    log_header(f"TNC Testbed — eval прогон · {len(domains)} доменов · режим {mode}")
    results, skipped = [], []
    for i, domain in enumerate(domains, 1):
        log_step(f"[{i}/{len(domains)}] {domain}", emoji="🧪")
        # изоляция доменов: один битый домен НЕ роняет весь прогон (ревью 2026-07-13);
        # SystemExit ловим отдельно — step2_scan.run делает sys.exit(1) на нечитаемом файле
        try:
            r = run_domain(domain, run_dir, skip_scan=args.skip_scan)
        except (Exception, SystemExit) as e:
            log_error(f"{domain}: сбой прогона — {type(e).__name__}: {str(e)[:200]}")
            r = {"domain": domain, "skipped": f"сбой: {type(e).__name__}: {str(e)[:120]}"}
        (skipped if "skipped" in r else results).append(r)

    if not results:
        log_warn("Ни одного домена с эталоном — сравнивать нечего")
        for r in skipped:
            log_warn(f"  {r['domain']}: {r['skipped']}")
        return 1

    total = write_scorecard(run_dir, results, skipped, mode, commit)
    if args.skip_scan:
        # отладочный режим по лежащим (возможно stale) step2 — в кривую доверия не пишем
        log_info("--skip-scan: строка в golden/history.csv НЕ добавлена (отладка, не реальный скан)")
    else:
        append_history(total, mode, results, commit)
        log_info("Строка добавлена в golden/history.csv — не забудь закоммитить вместе с фиксом")

    return 1 if total["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
