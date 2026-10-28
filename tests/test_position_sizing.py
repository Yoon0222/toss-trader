"""Position fraction and exact legacy upgrade checks use temporary paper ledgers."""
import json
from decimal import Decimal as D
import pytest
from test_paper_runner import runner as kr_runner, moment as kr_moment
from test_us_paper_runner import runner as us_runner, moment as us_moment


@pytest.mark.parametrize("market",["KR","US"])
async def test_half_equity_entry_includes_costs_and_caps_cash(tmp_path,market):
    factory,now=(kr_runner,kr_moment()) if market=="KR" else (us_runner,us_moment())
    engine,quotes=factory(tmp_path,now)
    initial=D(engine.ledger.snapshot()["cash"])
    await engine.tick(now)
    state=engine.ledger.snapshot()
    first=state["fills"][0]
    spent=D(first["gross"])+D(first["fee"])
    assert initial*D("0.45") < spent <= initial*D("0.5")
    assert D(state["cash"])==initial-spent
    rules=engine.snapshot()["rules"]
    assert rules["position_fraction"]=="0.5"
    assert D(rules["per_symbol"])==engine.entry_budget(state)
    # Marked positions change the equity-based limit, while cash remains a hard cap.
    sample={"cash":"100","positions":[{"quantity":2,"last_price":"10"}]}
    assert engine.entry_budget(sample)==D("60")
    sample["positions"][0]["last_price"]="200"
    assert engine.entry_budget(sample)==D("100")


@pytest.mark.parametrize("market",["KR","US"])
async def test_exact_fixed_cap_metadata_upgrades_preserving_history(tmp_path,market):
    us=market=="US"
    engine,quotes=(us_runner(tmp_path,us_moment(day=25)) if us else kr_runner(tmp_path,kr_moment(day=26)))
    await engine.start()
    await engine.close()
    ledger=engine.ledger
    config=json.loads(ledger.get_meta("experiment"))
    old=dict(config,per_symbol="1000" if us else "1000000")
    old.pop("position_fraction")
    strategy=json.loads(ledger.get_meta("strategy"))
    previous=dict(strategy,max_hold_minutes=90,version="us-volume-breakout-v2" if us else "volume-breakout-v1")
    previous.pop("position_fraction")
    ledger.set_meta("experiment",json.dumps(old))
    ledger.set_meta("strategy",json.dumps(previous))
    ledger.event("history","preserve me",quotes.at.isoformat())
    before=ledger.snapshot()
    await engine.start()
    try:
        assert engine.phase=="scheduled"
        after=engine.ledger.snapshot()
        assert after["cash"]==before["cash"] and after["fills"]==before["fills"]
        assert json.loads(engine.ledger.get_meta("experiment"))["position_fraction"]=="0.5"
        assert any(e["kind"]=="position_sizing_changed" for e in after["events"])
    finally:await engine.close()
    # Unrelated legacy setting changes must still fail startup without rewriting metadata.
    bad=dict(old,daily_loss="999999")
    ledger.set_meta("experiment",json.dumps(bad))
    await engine.start()
    assert engine.phase=="error"
    assert json.loads(ledger.get_meta("experiment"))==bad
