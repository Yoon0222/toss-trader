"""Removal of timed exits preserves protective exits and durable trade history."""
import json
from decimal import Decimal as D
import pytest
from test_paper_runner import runner as kr_runner, moment as kr_moment, calendar as kr_calendar
from test_us_paper_runner import runner as us_runner, moment as us_moment, calendar as us_calendar
from app.paper_runner import session_window
from app.us_paper_runner import us_session_window


@pytest.mark.parametrize("market",["KR","US"])
@pytest.mark.parametrize("trigger",[None,"stop_loss","trailing_stop","partial_take","end_of_day"])
async def test_after_ninety_minutes_only_price_or_session_exits(tmp_path,market,trigger):
    us=market=="US"
    moment=us_moment if us else kr_moment
    engine,quotes=(us_runner(tmp_path,moment()) if us else kr_runner(tmp_path,moment()))
    price=D(quotes.price)
    at=moment()
    engine.ledger.execute(key="opened",symbol="TEST",name="test",side="BUY",quantity=10,price=price,observed_at=at.isoformat(),received_at=at.isoformat(),reason="entry",fee_rate=D(0),tax_rate=D(0),at=at.isoformat())
    quotes.at=moment(hour=12)
    if trigger=="stop_loss":quotes.price=str(price*D("0.98"))
    if trigger=="trailing_stop":engine.ledger.mark("TEST",price*D("1.05"))
    if trigger=="partial_take":quotes.price=str(price*D("1.04"))
    if trigger=="end_of_day":quotes.at=moment(hour=15,minute=31 if us else 10)
    window=us_session_window(us_calendar(quotes.at),quotes.at) if us else session_window(kr_calendar(quotes.at),quotes.at)
    position=engine.ledger.snapshot()["positions"][0]
    await engine.manage_position(position,quotes.at,window)
    state=engine.ledger.snapshot()
    assert engine.snapshot()["rules"]["max_hold_minutes"] is None
    if trigger is None:
        assert len(state["fills"])==1 and state["positions"][0]["quantity"]==10
    else:
        assert state["fills"][0]["reason"]==trigger
        assert state["fills"][0]["side"]=="SELL"


@pytest.mark.parametrize("market",["KR","US"])
async def test_exact_timed_policy_upgrade_audited_once_preserves_fills(tmp_path,market):
    us=market=="US"
    at=us_moment(day=25) if us else kr_moment(day=26)
    engine,quotes=us_runner(tmp_path,at) if us else kr_runner(tmp_path,at)
    await engine.start();await engine.close()
    ledger=engine.ledger
    policy=json.loads(ledger.get_meta("strategy"))
    previous=dict(policy,version="us-volume-breakout-v3" if us else "volume-breakout-v2",max_hold_minutes=90)
    ledger.set_meta("strategy",json.dumps(previous))
    common=dict(symbol="OLD",name="history",quantity=1,observed_at=at.isoformat(),received_at=at.isoformat(),fee_rate=D(0),tax_rate=D(0),at=at.isoformat())
    ledger.execute(key="oldbuy",side="BUY",price=D(100),reason="entry",**common)
    ledger.execute(key="oldsell",side="SELL",price=D(100),reason="max_hold",**common)
    before=ledger.snapshot()
    for _ in range(2):
        await engine.start()
        try:
            assert engine.phase=="scheduled"
            after=engine.ledger.snapshot()
            assert after["cash"]==before["cash"] and after["fills"]==before["fills"]
            assert json.loads(engine.ledger.get_meta("strategy"))["max_hold_minutes"] is None
            assert sum(e["kind"]=="max_hold_removed" for e in after["events"])==1
        finally:await engine.close()
    bad=dict(previous,stop="-5%")
    ledger.set_meta("strategy",json.dumps(bad))
    await engine.start()
    assert engine.phase=="error"
    assert json.loads(ledger.get_meta("strategy"))==bad
