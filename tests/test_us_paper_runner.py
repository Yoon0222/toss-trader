from datetime import datetime, date, timedelta
from decimal import Decimal as D
from types import SimpleNamespace
import pytest
from app.paper_ledger import PaperLedger
from app.us_paper_runner import USPaperValidation, NY, KST, us_session_window, us_breakout_signal


def moment(hour=10, minute=0, day=26, month=10):
    return datetime(2026,month,day,hour,minute,tzinfo=NY)


def calendar(at,holiday=False):
    local=at.astimezone(NY)
    return {'today':{'date':str(local.date()),'regularMarket':None if holiday else {'startTime':local.replace(hour=9,minute=30).isoformat(),'endTime':local.replace(hour=16,minute=0).isoformat()}}}


def candles(at):
    rows=[]
    for index in range(22):
        rows.append({'timestamp':(at-timedelta(minutes=21-index)).isoformat(),'currency':'USD','openPrice':'100','highPrice':'100.2','lowPrice':'99.9','closePrice':'100.1','volume':'10000'})
    rows[-1].update(openPrice='100.1',highPrice='101.1',closePrice='101',volume='30000')
    return rows


class Market:
    def __init__(self,at):
        self.at=at;self.holiday=False;self.price='101';self.currency='USD';self.calls=[];self.quote_volume='10000';self.advance=None
        self.stock={'code':'US-AAPL','name':'test','market':'NASDAQ','currency':'USD','security_type':'STOCK','price':'101','change':'5','turnover':'20000000'}
    async def get(self,path,params=None,**kwargs):
        self.calls.append((path,params))
        if path=='/api/v1/market-calendar/US':return calendar(self.at,self.holiday)
        if path=='/api/v1/candles':
            assert params['adjusted'] is False
            return {'candles':candles(self.at),'nextBefore':None}
        if path=='/api/v1/orderbook':
            if self.advance:self.at=self.advance
            return {'timestamp':self.at.isoformat(),'currency':self.currency,'asks':[{'price':self.price,'volume':self.quote_volume}],'bids':[{'price':str(D(self.price)-D('.01')),'volume':self.quote_volume}]}
        raise AssertionError(path)
    async def rankings(self,**kwargs):
        assert kwargs['market_country']=='US'
        return {'observed_at':self.at.isoformat(),'stocks':[dict(self.stock)]}


def runner(tmp_path,at):
    settings=SimpleNamespace(us_paper_enabled=True,us_paper_data_dir=tmp_path,us_paper_start_date=date(2026,10,26),us_paper_end_date=date(2026,10,30))
    market=Market(at);engine=USPaperValidation(market,settings,clock=lambda:market.at)
    engine.ledger=PaperLedger(tmp_path/'ledger.sqlite3',D('10000'))
    return engine,market


def test_dst_and_midnight_seoul_session_dates():
    before=moment(day=30);after=moment(day=2,month=11)
    first=us_session_window(calendar(before),before)
    second=us_session_window(calendar(after),after)
    assert first['open'].astimezone(KST).hour==22
    assert second['open'].astimezone(KST).hour==23
    midnight=moment(hour=12).astimezone(KST)
    assert midnight.date()==date(2026,10,27)
    assert us_session_window(calendar(midnight),midnight)['open'].date()==date(2026,10,26)


def test_end_timestamp_and_regular_session_candles_only():
    at=moment();window=us_session_window(calendar(at),at)
    assert us_breakout_signal(candles(at),at,window)
    assert us_breakout_signal(candles(at),at-timedelta(seconds=1),window) is None
    rows=candles(at)
    for row in rows:row['currency']='KRW'
    assert us_breakout_signal(rows,at,window) is None
    early=moment(hour=9,minute=45)
    assert us_breakout_signal(candles(early),early,window) is None


async def test_entry_restart_usd_ny_day_and_quote_only(tmp_path):
    engine,market=runner(tmp_path,moment(hour=12).astimezone(KST))
    await engine.tick(market.at)
    state=engine.snapshot()
    assert state['currency']=='USD' and state['market']=='US'
    fills=state['paper_account']['fills'];assert len(fills)==1
    assert fills[0]['at'].startswith('2026-10-26T12:00')
    assert D(fills[0]['gross'])+D(fills[0]['fee'])<=5000
    assert D(fills[0]['tax'])==0
    assert engine.ledger.daily_stats('2026-10-26')['entry_count']==1
    engine.ledger=PaperLedger(tmp_path/'ledger.sqlite3',D('10000'))
    await engine.tick(market.at)
    assert len(engine.ledger.snapshot()['fills'])==1
    assert not any('/orders' in path for path,_ in market.calls)


@pytest.mark.parametrize('change',[{'currency':'KRW'},{'price':'4.99'},{'market':'KOSDAQ'},{'turnover':'9999999'},{'security_type':'ETF'},{'security_type':None},{'change':'11'}])
async def test_universe_gates(tmp_path,change):
    engine,market=runner(tmp_path,moment());market.stock.update(change)
    await engine.tick(market.at)
    assert not engine.ledger.snapshot()['fills']


async def test_holiday_and_wrong_quote_currency(tmp_path):
    engine,market=runner(tmp_path,moment());market.holiday=True
    await engine.tick(market.at);assert engine.phase=='holiday'
    market.holiday=False;market.currency='KRW'
    await engine.tick(market.at);assert not engine.ledger.snapshot()['fills']


async def test_stop_protective_exit_while_paused(tmp_path):
    engine,market=runner(tmp_path,moment());await engine.tick(market.at)
    engine.pause();market.at+=timedelta(minutes=1);market.price='99'
    await engine.tick(market.at)
    state=engine.ledger.snapshot();assert not state['positions']
    assert state['fills'][0]['reason']=='stop_loss'
    assert D(state['fills'][0]['fee'])>0 and D(state['fills'][0]['tax'])==0


async def test_partial_small_liquidity_then_eod(tmp_path):
    engine,market=runner(tmp_path,moment());await engine.tick(market.at)
    engine.pause();market.price='105';market.quote_volume='10'
    for _ in range(engine.ledger.snapshot()['positions'][0]['quantity']//2):
        market.at+=timedelta(minutes=1);await engine.tick(market.at)
    assert engine.ledger.snapshot()['positions'][0]['partial_taken'] is True
    market.quote_volume='10000';market.at=moment(hour=15,minute=31)
    await engine.tick(market.at);assert not engine.ledger.snapshot()['positions']
    assert engine.ledger.snapshot()['fills'][0]['reason']=='end_of_day'


async def test_no_fill_after_quote_await_crosses_close(tmp_path):
    engine,market=runner(tmp_path,moment());market.advance=moment(hour=16)
    await engine.tick(market.at);assert not engine.ledger.snapshot()['fills']


async def test_expiry_uses_ny_date_and_final_report(tmp_path):
    engine,market=runner(tmp_path,moment(day=30,hour=12).astimezone(KST))
    await engine.tick(market.at);assert engine.ledger.snapshot()['positions']
    market.at=moment(day=30,hour=16).astimezone(KST)
    await engine.tick(market.at)
    report=engine.ledger.snapshot()['reports'][0]['report']
    assert report['day']=='2026-10-30' and report['currency']=='USD' and report['final']
    assert report['unliquidated'] is True
    market.at=moment(day=31)
    await engine.tick(market.at);assert engine.phase=='completed_with_positions'
    assert engine.resume()['status']=='expired'


async def test_preflight_displays_ny_and_seoul(tmp_path):
    engine,market=runner(tmp_path,moment());result=await engine.preflight()
    assert result['status']=='connected' and result['market_open'].endswith('-04:00')
    assert result['market_open_kst'].startswith('2026-10-26T22:30')
    assert result['market_close_kst'].startswith('2026-10-27T05:00')


async def test_daily_loss_and_entry_pause_persist(tmp_path):
    engine,market=runner(tmp_path,moment())
    engine.pause();await engine.tick(market.at)
    assert engine.phase=='paused_entries' and not engine.ledger.snapshot()['fills']
    engine.resume();engine.ledger.set_meta('day_start:2026-10-26','10201')
    await engine.tick(market.at)
    assert engine.phase=='loss_limit' and not engine.ledger.snapshot()['fills']
    engine.ledger=PaperLedger(tmp_path/'ledger.sqlite3',D('10000'))
    assert engine.ledger.get_meta('loss_stop:2026-10-26')=='true'


async def test_before_start_after_end_never_request_quotes(tmp_path):
    engine,market=runner(tmp_path,moment(day=25))
    await engine.tick(market.at);assert engine.phase=='scheduled' and not market.calls
    market.at=moment(day=31)
    await engine.tick(market.at);assert engine.phase=='completed' and not market.calls


async def test_regular_open_five_minute_delay(tmp_path):
    engine,market=runner(tmp_path,moment(hour=9,minute=34))
    await engine.tick(market.at)
    assert engine.phase=='waiting_entry'
    assert not any(path=='/api/v1/candles' for path,_ in market.calls)


async def test_pause_during_await_prevents_pending_entry(tmp_path):
    engine,market=runner(tmp_path,moment())
    original=market.get
    async def pausing(path,*args,**kwargs):
        value=await original(path,*args,**kwargs)
        if path=='/api/v1/orderbook':engine.pause()
        return value
    market.get=pausing
    await engine.tick(market.at)
    assert not engine.ledger.snapshot()['fills']


async def test_executable_price_below_five_rejected(tmp_path):
    engine,market=runner(tmp_path,moment())
    market.price='4.99'
    await engine.tick(market.at)
    assert not engine.ledger.snapshot()['fills']


@pytest.mark.parametrize('bad', ['capital','orphan','currency','kr_experiment'])
async def test_start_refuses_mixed_or_unidentified_ledger(tmp_path,bad):
    engine,market=runner(tmp_path,moment())
    if bad=='capital':
        import sqlite3
        with sqlite3.connect(tmp_path/'ledger.sqlite3') as db:
            db.execute("UPDATE state SET initial_cash='10000000',cash='10000000'")
    elif bad=='orphan':
        engine.ledger.event('old','unidentified activity',moment().isoformat())
    elif bad=='currency':engine.ledger.set_meta('currency','KRW')
    elif bad=='kr_experiment':engine.ledger.set_meta('experiment','{"initial_cash":"10000000"}')
    await engine.start()
    assert engine.phase=='error' and engine.ledger is None and engine.task is None
    reopened=PaperLedger(tmp_path/'ledger.sqlite3')
    assert reopened.get_meta('market') is None
    assert reopened.get_meta('currency')==('KRW' if bad=='currency' else None)


async def test_new_empty_us_identity_and_restart(tmp_path):
    engine,market=runner(tmp_path,moment())
    await engine.start()
    try:
        assert engine.ledger.get_meta('market')=='US'
        assert engine.ledger.get_meta('currency')=='USD'
    finally:await engine.close()
    engine,market=runner(tmp_path,moment())
    await engine.start()
    try:assert engine.phase=='scheduled'
    finally:await engine.close()


async def test_slow_nonsignal_scan_budget_rotates_without_sleep(tmp_path,monkeypatch):
    import app.us_paper_runner as module
    engine,market=runner(tmp_path,moment())
    simulated=[0.0]
    monkeypatch.setattr(module.time,'monotonic',lambda:simulated[0])
    async def ranking(**kwargs):
        return {'observed_at':market.at.isoformat(),'stocks':[dict(market.stock,code=f'US-{i}') for i in range(100)]}
    market.rankings=ranking
    original=market.get
    scanned=[]
    async def slow(path,params=None,**kwargs):
        if path=='/api/v1/candles':
            scanned.append(params['symbol']);simulated[0]+=5
            rows=candles(market.at);rows[-1]['volume']='10000'
            return {'candles':rows,'nextBefore':None}
        return await original(path,params,**kwargs)
    market.get=slow
    await engine.tick(market.at)
    assert scanned==['US-0','US-1']
    assert engine.ledger.get_meta('scan_cursor')==2
    assert not engine.ledger.snapshot()['fills']
    await engine.tick(market.at)
    assert scanned==['US-0','US-1','US-2','US-3']



def seed_loss(engine, symbol, at, index=0, reason="stop_loss"):
    kwargs=dict(symbol=symbol,name=symbol,quantity=1,observed_at=at.isoformat(),received_at=at.isoformat(),fee_rate=D("0.001"),tax_rate=D(0),at=at.isoformat())
    engine.ledger.execute(key=f"seed-buy-{index}",side="BUY",price=D(100),reason="entry",**kwargs)
    engine.ledger.execute(key=f"seed-sell-{index}",side="SELL",price=D(99),reason=reason,**kwargs)


async def test_three_losses_rotate_to_other_symbol_after_restart(tmp_path):
    engine,market=runner(tmp_path,moment())
    for index in range(3):seed_loss(engine, f"LOST-{index}", market.at, index)
    engine.phase="loss_limit"
    engine.ledger=PaperLedger(tmp_path/'ledger.sqlite3',D('10000'))
    async def ranking(**kwargs):
        return {'observed_at':market.at.isoformat(),'stocks':[dict(market.stock,code="LOST-0"),dict(market.stock,code="NEW")]}
    market.rankings=ranking
    await engine.tick(market.at)
    state=engine.snapshot()
    assert state['paper_account']['positions'][0]['symbol']=='NEW'
    assert state['excluded_symbols']==['LOST-0','LOST-1','LOST-2']
    assert state['rules']['consecutive_loss_stop'] is False
    assert state['rules']['daily_entry_limit']==5
    assert not any(params and params.get('symbol')=='LOST-0' for path,params in market.calls)
    assert engine.ledger.daily_stats('2026-10-26')['entry_count']==4


async def test_five_entry_cap_still_blocks_after_losses(tmp_path):
    engine,market=runner(tmp_path,moment())
    for index in range(5):seed_loss(engine,f"LOSS-{index}",market.at,index)
    await engine.tick(market.at)
    assert engine.phase=='daily_entry_limit'
    assert not engine.ledger.snapshot()['positions']
    assert engine.ledger.daily_stats('2026-10-26')['entry_count']==5


def test_loss_exclusion_reads_full_history_and_ny_day(tmp_path):
    engine,market=runner(tmp_path,moment(hour=12).astimezone(KST))
    at=market.at.astimezone(NY)
    seed_loss(engine,'EARLY',at,0,reason='max_hold')
    for index in range(1,53):seed_loss(engine,f'LOSS-{index}',at,index)
    assert len(engine.ledger.snapshot()['fills'])==100
    assert 'EARLY' in engine.excluded_symbols(date(2026,10,26))
    assert not engine.excluded_symbols(date(2026,10,27))


async def test_exact_v1_strategy_upgrades_without_reset_and_rejects_other_change(tmp_path):
    import json
    engine,market=runner(tmp_path,moment())
    # Keep the background worker outside the validation period while testing startup.
    market.at=moment(day=25)
    await engine.start()
    await engine.close()
    strategy=json.loads(engine.ledger.get_meta('strategy'))
    legacy=dict(strategy,version='us-volume-breakout-v1',max_consecutive_losses=3,max_hold_minutes=90)
    legacy.pop('exclude_losing_symbols_same_day')
    legacy.pop('position_fraction')
    engine.ledger.set_meta('strategy',json.dumps(legacy))
    seed_loss(engine,'HISTORY',moment(),1)
    before=engine.ledger.snapshot()['cash']
    await engine.start()
    try:
        assert engine.phase=='scheduled'
        assert engine.ledger.snapshot()['cash']==before
        assert json.loads(engine.ledger.get_meta('strategy'))['version']=='us-volume-breakout-v4'
        assert any(e['kind']=='strategy_policy_changed' for e in engine.ledger.snapshot()['events'])
    finally:await engine.close()
    changed=dict(legacy,daily_entries=6)
    engine.ledger.set_meta('strategy',json.dumps(changed))
    await engine.start()
    assert engine.phase=='error' and engine.ledger is None



def test_loss_exclusion_normalizes_legacy_seoul_timestamp_to_ny_date(tmp_path):
    engine,market=runner(tmp_path,moment(hour=12))
    # Seoul is already October 27; this is still the October 26 US session.
    seoul_at=moment(hour=12).astimezone(KST)
    assert seoul_at.date()==date(2026,10,27)
    seed_loss(engine,'LEGACY-KST',seoul_at,100)
    seed_loss(engine,'CURRENT-NY',moment(hour=12),101)
    seed_loss(engine,'NEXT-SESSION',moment(hour=12,day=27),102)
    assert engine.excluded_symbols(date(2026,10,26))=={'LEGACY-KST','CURRENT-NY'}
    assert engine.excluded_symbols(date(2026,10,27))=={'NEXT-SESSION'}
