"""
pytest: пинит исходы политики по 5 ad set'ам — чтобы багфиксы не регрессировали.
Запуск:  cd 03-execution/engine && python -m pytest -q
"""
import policy

RESULTS = {r["adset"].split(" ")[0]: r for r in policy.run_portfolio()}


def test_A_winner_scales():
    assert RESULTS["A"]["action"] == "scale"


def test_B_bleeding_goes_to_human_as_creative():
    # был баг #1: уходил в reconcile. Теперь диагностируется как креатив → human.
    assert RESULTS["B"]["action"] == "send_to_human"
    assert "креатив" in RESULTS["B"]["rationale"]


def test_C_learning_holds_not_human():
    # был баг #2: мог уйти в send_to_human раньше learning-hold.
    assert RESULTS["C"]["action"] == "hold"
    assert RESULTS["C"]["route"].startswith("python")


def test_D_attribution_trap_holds():
    # был yellow flag: D ("ловушка атрибуции") уходил в site-диссонанс ПЕРЕД attribution
    # и сам кейс атрибуцию не проверял. Теперь attribution-maturity первой → D холдится (wait).
    assert RESULTS["D"]["action"] == "hold"
    assert "attribution" in RESULTS["D"]["rationale"]
    assert RESULTS["D"]["route"].startswith("python")


def test_E_creative_fatigue_to_human():
    assert RESULTS["E"]["action"] == "send_to_human"
    assert "креатив" in RESULTS["E"]["rationale"]


def test_no_action_ever_deletes():
    # железобетон: ни одно действие не равно delete.
    for r in RESULTS.values():
        assert r["action"] in {"scale", "hold", "pause_candidate", "send_to_human", "do_nothing"}


def test_site_dissonance_path_still_covered():
    # «CTR↑/CVR↓ при ЗРЕЛОМ окне → сканер 01 → human» больше не покрыт портфельным моком
    # (D ушёл в attribution-hold) — пиним путь точечно на decide().
    from schema import AdSet, CampaignState, Signal
    a = AdSet("t", "camp_1", "T · site", 1500, 2000, 20, 90, 0.6, 2.5, 0.6,
              "done", 10, 250, 2, anchor_in_stock=True)
    sigs = {
        "dissonance": Signal("dissonance", "site", "CTR↑/CVR↓"),
        "attribution": Signal("attribution", "mature", "окно закрыто"),
        "utility": Signal("utility", 0.5, "низ"),
        "budget": Signal("budget", "не выбирает", "2/5"),
        "saturation": Signal("saturation", "растёт", ""),
        "offer": Signal("offer", "ок", ""),
    }
    c = CampaignState("camp_1", 30.0, 60000, 2000, 14780, 412, 8, 30)
    d, _, route = policy.decide(a, sigs, c)
    assert d.action == "send_to_human" and "сканер" in d.rationale and route == "human"


def test_cbo_suppresses_adset_scale():
    # Reserved-правило: эффективный ad set, который при ABO ушёл бы в scale, при CBO
    # НЕ скейлится на уровне ad set → уходит на campaign-level (бюджетом рулит кампания).
    from schema import AdSet, CampaignState, Signal
    a = AdSet("t", "camp_1", "T · eff", 1000, 2000, 50, 20, 3.0, 3.0, 2.5,
              "done", 10, 200, 2, anchor_in_stock=True)
    sigs = {
        "dissonance": Signal("dissonance", "ok", ""),
        "attribution": Signal("attribution", "mature", ""),
        "utility": Signal("utility", 1.8, ""),
        "budget": Signal("budget", "выбирает", "5/5"),
        "saturation": Signal("saturation", "растёт", ""),
        "offer": Signal("offer", "ок", ""),
    }
    c = CampaignState("camp_1", 30.0, 60000, 2000, 14780, 412, 8, 30, budget_mode="CBO")
    d, _, route = policy.decide(a, sigs, c)
    assert d.action == "send_to_human" and "CBO" in d.rationale and "campaign" in route
