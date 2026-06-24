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


def test_D_site_dissonance_scanner_then_human():
    assert RESULTS["D"]["action"] == "send_to_human"
    assert "сканер" in RESULTS["D"]["rationale"]


def test_E_creative_fatigue_to_human():
    assert RESULTS["E"]["action"] == "send_to_human"
    assert "креатив" in RESULTS["E"]["rationale"]


def test_no_action_ever_deletes():
    # железобетон: ни одно действие не равно delete.
    for r in RESULTS.values():
        assert r["action"] in {"scale", "hold", "pause_candidate", "send_to_human", "do_nothing"}
