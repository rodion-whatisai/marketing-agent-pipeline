"""
TNC Testbed — witness_check: «эталон не противоречит сырому трафику свидетеля»
===============================================================================
Машинная перекрёстная проверка (см. TESTBED-PLAN.md, «Свидетель»). Сравнивает
правда-факты эталона golden/expected_<domain>.json с записями свидетеля
golden/<domain>/witness_*.json (режим pages) и witness_journey_*.json.

Три исхода на факт:
- ✅ ПОДТВЕРЖДЕНО   — у свидетеля есть сырой запрос, подтверждающий факт
- ⚠ НЕ ЗАСВИДЕТЕЛЬСТВОВАНО — трафик-улики нет ни за, ни против (например
  html-only детекция или страница без witness-записи) → факт выносится на гейт
- ❌ ПРОТИВОРЕЧИЕ   — трафик свидетеля прямо противоречит эталону
  (например forbidden-платформа реально шлёт запросы) → exit 1

Правило гейта не отменяется: witness_check — механика, правдой факт делает
только «да» Rodion'а.

Запуск: python witness_check.py <domain> [<domain2> ...]
        python witness_check.py            # все домены, у которых есть эталон+свидетель
"""

import sys
import argparse
from pathlib import Path

from utils import setup_console
setup_console()

from log import log_error, log_success, log_info, log_warn, log_header
import eval_lib as ev

# Уровни исходов
OK = "ПОДТВЕРЖДЕНО"
UNWITNESSED = "НЕ ЗАСВИДЕТЕЛЬСТВОВАНО"
CONTRADICTION = "ПРОТИВОРЕЧИЕ"

# Для присутствия платформы SDK-загрузка не считается: скрипт может лежать,
# но не стрелять. Meta подтверждается ТОЛЬКО событием на facebook.com/tr.
PRESENCE_OVERRIDES = {
    "Meta": ["facebook.com/tr"],
}

# Явный host-словарь для внешних сервисов корпуса (НЕ подстроки имён — привет Cal.com)
SERVICE_HOSTS = {
    "Calendly": ["calendly.com"],
    "Cal.com": ["app.cal.com", "api.cal.com", "//cal.com"],
    "Stripe": ["stripe.com", "js.stripe.com"],
    "Zendesk": ["zendesk.com", "zdassets.com"],
    "Typeform": ["typeform.com"],
    "HubSpot": ["hsforms.com", "hs-scripts.com", "hubspot.com"],
    "Intercom": ["intercom.io", "intercomcdn.com"],
    "Klaviyo": ["klaviyo.com"],
    "Mailchimp": ["list-manage.com", "chimpstatic.com"],
    "Crisp": ["crisp.chat"],
}


def _presence_probes(platform: str) -> list:
    plat = ev.canonical_platform(platform)
    return PRESENCE_OVERRIDES.get(plat) or ev.EVIDENCE_PROBES.get(plat) or []


def _witness_page(witnesses: list, path: str) -> dict:
    """Последняя (по дате) witness-запись страницы по path."""
    for w in sorted(witnesses, key=lambda x: x.get("date", ""), reverse=True):
        for pg in w.get("pages", []):
            if pg.get("path") == path:
                return pg
    return None


def _domain_requests(witnesses: list, journeys: list):
    """Все pixel-запросы домена: из pages-свидетелей (с path) и journey (с phase)."""
    for w in witnesses:
        for pg in w.get("pages", []):
            for r in pg.get("pixel_requests", []):
                yield {**r, "where": f"page:{pg.get('path')}"}
    for j in journeys:
        for r in j.get("captured", []):
            yield {**r, "where": f"journey:{r.get('phase', '?')}"}


def _find(fact_name, level, page, detail):
    return {"fact": fact_name, "level": level, "page": page, "detail": detail}


def check_domain(expected: dict, witnesses: list, journeys: list) -> list:
    """Чистая функция: список исходов по каждому правда-факту эталона."""
    findings = []
    domain_reqs = list(_domain_requests(witnesses, journeys))
    consent_ok = any((w.get("consent") or {}).get("clicked") for w in witnesses)

    for path, epage in (expected.get("pages") or {}).items():
        wpage = _witness_page(witnesses, path)
        page_reqs = (wpage or {}).get("pixel_requests", [])

        # платформы, заявленные эталоном как присутствующие
        for plat in ev.canonical_set(epage.get("platforms_detected")):
            probes = _presence_probes(plat)
            hits = [r for r in page_reqs if any(p in r.get("url", "") for p in probes)]
            if not hits:   # страница молчит — journey мог видеть (домен-уровень)
                hits = [r for r in domain_reqs if any(p in r.get("url", "") for p in probes)]
            if hits:
                h = hits[0]
                findings.append(_find(f"platform:{plat}", OK, path,
                                      f"{h.get('method', '?')} {h.get('url', '')[:100]} [{h.get('where', 'page')}]"))
            elif wpage is None:
                findings.append(_find(f"platform:{plat}", UNWITNESSED, path,
                                      "страница без witness-записи — прогони witness --pages"))
            else:
                findings.append(_find(f"platform:{plat}", UNWITNESSED, path,
                                      "трафика к платформе свидетель не видел (html-only детекция?) — на гейт"))

        # запреты: у свидетеля должен быть НОЛЬ хитов
        for plat in ev.canonical_set(epage.get("platforms_forbidden")):
            probes = _presence_probes(plat)
            hits = [r for r in domain_reqs if any(p in r.get("url", "") for p in probes)]
            if hits:
                h = hits[0]
                findings.append(_find(f"forbidden:{plat}", CONTRADICTION, path,
                                      f"эталон запрещает, а свидетель видел: {h.get('method', '?')} "
                                      f"{h.get('url', '')[:100]} [{h.get('where')}]"))
            elif not consent_ok:
                findings.append(_find(f"forbidden:{plat}", UNWITNESSED, path,
                                      "consent не кликнут — отсутствие трафика не доказывает отсутствие платформы"))
            else:
                findings.append(_find(f"forbidden:{plat}", OK, path, "ноль запросов у свидетеля (consent кликнут)"))

        # события, которые обязаны лететь
        for evt in epage.get("conversion_events_min") or []:
            evt = ev.canonical_event(evt)
            token = evt.split(":", 1)[-1]
            hits = [r for r in domain_reqs
                    if token == r.get("query_event") or token in (r.get("body_events") or [])]
            if hits:
                h = hits[0]
                findings.append(_find(f"event:{evt}", OK, path,
                                      f"{h.get('method', '?')} {h.get('url', '')[:80]} [{h.get('where')}]"))
            else:
                findings.append(_find(f"event:{evt}", UNWITNESSED, path,
                                      "событие в трафике свидетеля не найдено — на гейт"))

        # внешние сервисы — по host-словарю в third_party_hosts
        whosts = set((wpage or {}).get("third_party_hosts") or [])
        for svc in epage.get("external_services") or []:
            probes = SERVICE_HOSTS.get(svc)
            if not probes:
                findings.append(_find(f"service:{svc}", UNWITNESSED, path,
                                      f"нет host-записи для {svc!r} в SERVICE_HOSTS — на гейт"))
                continue
            hit = next((h for h in whosts for p in probes if p in h), None)
            if hit:
                findings.append(_find(f"service:{svc}", OK, path, f"хост в трафике: {hit}"))
            elif wpage is None:
                findings.append(_find(f"service:{svc}", UNWITNESSED, path, "страница без witness-записи"))
            else:
                findings.append(_find(f"service:{svc}", UNWITNESSED, path,
                                      "хостов сервиса в трафике нет (html-only детекция?) — на гейт"))

        # redirect/HTTP-факты свидетеля vs статус эталона (заготовка known-fail)
        if wpage:
            st = ev.normalize_status(epage.get("status", ""))
            if wpage.get("redirected") and st not in ("REDIRECTED", "MISSING"):
                findings.append(_find("redirect", UNWITNESSED, path,
                                      f"свидетель видел редирект → {wpage.get('final_url', '')[:80]}, "
                                      f"эталонный статус {st} — известный класс C8, на гейт"))
            if (wpage.get("http_status") or 200) >= 400 and st != "HTTP_ERROR":
                findings.append(_find("http_status", CONTRADICTION, path,
                                      f"свидетель получил HTTP {wpage['http_status']}, "
                                      f"а эталон ждёт {st} — страница мертва?"))

    return findings


def run(domains: list) -> int:
    exit_code = 0
    for domain in domains:
        exp_path = ev.expected_path(domain)
        if not exp_path.exists():
            log_warn(f"{domain}: эталона нет — пропуск")
            continue
        gdir = ev.GOLDEN_DIR / domain
        witnesses = [ev.load_json(p) for p in sorted(gdir.glob("witness_2*.json"))]
        journeys = [ev.load_json(p) for p in sorted(gdir.glob("witness_journey_*.json"))]
        if not witnesses and not journeys:
            log_warn(f"{domain}: witness-записей нет — прогони witness.py --pages")
            continue

        findings = check_domain(ev.load_expected(domain), witnesses, journeys)
        n_ok = sum(1 for f in findings if f["level"] == OK)
        n_un = sum(1 for f in findings if f["level"] == UNWITNESSED)
        n_contra = sum(1 for f in findings if f["level"] == CONTRADICTION)

        log_header(f"{domain} — {n_ok} подтверждено, {n_un} не засвидетельствовано, {n_contra} противоречий")
        for f in findings:
            line = f"{f['page']:32} {f['fact']:28} {f['detail'][:110]}"
            if f["level"] == CONTRADICTION:
                log_error(line)
                exit_code = 1
            elif f["level"] == UNWITNESSED:
                log_warn(line)
            else:
                log_success(line, emoji="✔")
    return exit_code


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Проверка эталонов против сырого трафика свидетеля")
    ap.add_argument("domains", nargs="*", help="домены (дефолт: все с эталоном и свидетелем)")
    args = ap.parse_args()
    domains = args.domains
    if not domains:
        domains = sorted({p.stem.replace("expected_", "")
                          for p in ev.GOLDEN_DIR.glob("expected_*.json")})
    sys.exit(run(domains))
