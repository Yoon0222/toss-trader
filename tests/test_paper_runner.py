from datetime import datetime,timedelta,date
from decimal import Decimal as D
import pytest
from app.config import Settings
from app.paper_runner import PaperValidation, WorkerLock, KST, fresh, session_window, breakout_signal, executable_quote
from app.paper_ledger import PaperLedger

def moment(hour=10,minute=0,day=27):return datetime(2026,10,day,hour,minute,tzinfo=KST)
def calendar(at,holiday=False,late=False):
    day=at.date().isoformat()
    return {"today":{"date":day,"integrated":None if holiday else {"regularMarket":{"startTime":day+('T10:00:00+09:00' if late else 'T09:00:00+09:00'),"singlePriceAuctionStartTime":day+('T16:20:00+09:00' if late else 'T15:20:00+09:00'),"endTime":day+('T16:30:00+09:00' if late else 'T15:30:00+09:00')}}}}
def book(at,price="10100",volume="100000"):
    return {"timestamp":at.isoformat(),"currency":"KRW","asks":[{"price":price,"volume":volume}],"bids":[{"price":str(D(price)-10),"volume":volume}]}
def candles(at,signal=True):
    result=[]
    for i in range(22):
        stamp=at-timedelta(minutes=22-i)
        result.append({"timestamp":stamp.isoformat(),"openPrice":"10000","highPrice":"10040","lowPrice":"9990","closePrice":"10020","volume":"10000"})
    if signal:result[-1].update(openPrice="10020",highPrice="10110",closePrice="10100",volume="30000")
    return result
class Market:
    def __init__(self,at):self.at=at;self.calls=[];self.holiday=False;self.price="10100";self.fail=False
    async def get(self,path,params=None,**kwargs):
        self.calls.append(path)
        if self.fail:raise RuntimeError('test outage')
        if path=="/api/v1/market-calendar/KR":return calendar(self.at,self.holiday)
        if path=="/api/v1/candles":return {"candles":candles(self.at)}
        if path=="/api/v1/orderbook":return book(self.at,self.price)
        raise AssertionError(path)
    async def rankings(self,**kwargs):
        return {"observed_at":self.at.isoformat(),"stocks":[{"code":"0101N0","name":"test","market":"KOSDAQ","change":"5","turnover":"2000000000"}]}
def runner(tmp_path,at):
    settings=Settings(_env_file=None,paper_enabled=True,paper_data_dir=tmp_path,paper_start_date=date(2026,10,27),paper_end_date=date(2026,11,2))
    market=Market(at);engine=PaperValidation(market,settings,clock=lambda:market.at)
    engine.ledger=PaperLedger(tmp_path/"ledger.sqlite3")
    return engine,market

def test_freshness_boundaries():
    now=moment()
    assert fresh((now-timedelta(seconds=15)).isoformat(),now)
    assert not fresh((now-timedelta(seconds=16)).isoformat(),now)
    assert not fresh((now+timedelta(seconds=3)).isoformat(),now)

def test_official_calendar_and_late_open():
    now=moment()
    assert session_window(calendar(now,holiday=True),now) is None
    normal=session_window(calendar(now),now)
    assert normal['buy_start'].hour==9 and normal['buy_start'].minute==5
    assert normal['liquidate'].hour==15
    late=session_window(calendar(now,late=True),now)
    assert late['buy_start'].hour==10 and late['liquidate'].hour==16
    with pytest.raises(ValueError):session_window(calendar(now),moment(day=28))

def test_breakout_uses_completed_fresh_bars_only():
    now=moment();rows=candles(now)
    assert breakout_signal(rows,now)
    assert breakout_signal(candles(now,False),now) is None
    assert breakout_signal(rows,now+timedelta(minutes=4)) is None
    future={**rows[-1],"timestamp":(now+timedelta(minutes=10)).isoformat(),"closePrice":"999999"}
    assert breakout_signal(rows+[future],now)['close']=="10100"
    assert breakout_signal(rows[:10],now) is None

def test_fill_model_uses_book_side_depth_and_slippage():
    now=moment();data=book(now,volume="30")
    qty,buy=executable_quote(data,"BUY",100,now)
    assert qty==3 and buy>D("10100")
    qty,sell=executable_quote(data,"SELL",100,now)
    assert qty==3 and sell<D("10090")
    data['timestamp']=(now-timedelta(seconds=16)).isoformat()
    with pytest.raises(ValueError):executable_quote(data,"BUY",1,now)

def test_spread_and_crossed_book_block_entry():
    now=moment();data=book(now)
    data['bids'][0]['price']='9000'
    with pytest.raises(ValueError):executable_quote(data,"BUY",1,now)
    data['bids'][0]['price']='11000'
    with pytest.raises(ValueError):executable_quote(data,"BUY",1,now)

def test_worker_lock_prevents_second_instance(tmp_path):
    one,two=WorkerLock(tmp_path/'lock'),WorkerLock(tmp_path/'lock')
    assert one.acquire()
    try:assert not two.acquire()
    finally:one.close()
    assert two.acquire();two.close()

async def test_no_trade_before_start_and_after_period(tmp_path):
    engine,market=runner(tmp_path,moment(day=26))
    await engine.tick(market.at)
    assert engine.phase=='scheduled' and not market.calls
    market.at=datetime(2026,11,3,10,tzinfo=KST)
    await engine.tick(market.at)
    assert engine.phase=='completed'
    assert engine.ledger.snapshot()['reports'][-1]['report']['final'] is True
    assert not engine.ledger.snapshot()['fills']

async def test_holiday_and_before_entry_no_buy(tmp_path):
    engine,market=runner(tmp_path,moment(hour=9,minute=2))
    await engine.tick(market.at)
    assert not engine.ledger.snapshot()['fills']
    market.holiday=True
    await engine.tick(market.at)
    assert engine.phase=='holiday'

async def test_signal_executes_once_and_recovers(tmp_path):
    engine,market=runner(tmp_path,moment())
    await engine.tick(market.at)
    state=engine.ledger.snapshot()
    assert len(state['fills'])==1 and state['fills'][0]['side']=='BUY'
    assert D(state['initial_cash'])>D(state['cash'])
    assert D(state['fills'][0]['gross'])+D(state['fills'][0]['fee'])<=D('5000000')
    await engine.tick(market.at)
    assert len(engine.ledger.snapshot()['fills'])==1
    recovered=PaperLedger(tmp_path/'ledger.sqlite3')
    assert recovered.snapshot()['cash']==engine.ledger.snapshot()['cash']
    assert not any('orders' in path for path in market.calls)

async def test_paused_and_loss_limit_block_entries(tmp_path):
    engine,market=runner(tmp_path,moment())
    engine.pause();await engine.tick(market.at)
    assert engine.phase=='paused_entries'
    assert not engine.ledger.snapshot()['fills']
    engine.resume();engine.ledger.set_meta('day_start:2026-10-27','10300000')
    await engine.tick(market.at)
    assert engine.phase=='loss_limit'
    assert not engine.ledger.snapshot()['fills']

async def test_stop_loss_sells_even_when_entries_paused(tmp_path):
    engine,market=runner(tmp_path,moment())
    await engine.tick(market.at)
    engine.pause();market.at+=timedelta(minutes=1);market.price='9900'
    await engine.tick(market.at)
    state=engine.ledger.snapshot()
    assert not state['positions']
    assert state['fills'][0]['reason']=='stop_loss'
    assert D(state['realized_profit'])<0

async def test_end_of_day_liquidation_and_report(tmp_path):
    engine,market=runner(tmp_path,moment())
    await engine.tick(market.at)
    market.at=moment(hour=15,minute=1)
    await engine.tick(market.at)
    assert not engine.ledger.snapshot()['positions']
    market.at=moment(hour=15,minute=31)
    await engine.tick(market.at)
    state=engine.ledger.snapshot()
    assert state['reports'][0]['report']['unliquidated'] is False
    assert len(state['fills'])==2

async def test_quote_outage_leaves_position_unliquidated(tmp_path):
    engine,market=runner(tmp_path,moment())
    await engine.tick(market.at)
    market.at=moment(hour=15,minute=1);market.fail=True
    window=session_window(calendar(market.at),market.at)
    await engine.manage_position(engine.ledger.snapshot()['positions'][0],market.at,window)
    assert engine.ledger.snapshot()['positions']
    assert engine.error
    market.fail=False;market.at=moment(hour=15,minute=31)
    await engine.tick(market.at)
    assert engine.phase=='unliquidated'
    assert engine.ledger.snapshot()['reports'][0]['report']['unliquidated']
    await engine.tick(market.at)
    assert engine.phase=='unliquidated'

async def test_no_buy_if_orderbook_arrives_after_cutoff(tmp_path):
    engine,market=runner(tmp_path,moment(hour=14,minute=59))
    original=market.get
    async def delayed(path,*args,**kwargs):
        if path=='/api/v1/orderbook':market.at=moment(hour=15,minute=0)
        return await original(path,*args,**kwargs)
    market.get=delayed
    await engine.tick(moment(hour=14,minute=59))
    assert not engine.ledger.snapshot()['fills']

async def test_preflight_uses_official_calendar_and_preserves_state(tmp_path):
    engine,market=runner(tmp_path,moment())
    before=engine.ledger.snapshot()
    result=await engine.preflight()
    assert result["status"]=="connected"
    assert result["liquidation_start"].endswith("15:00:00+09:00")
    assert before==engine.ledger.snapshot()
