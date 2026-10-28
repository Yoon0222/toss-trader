from decimal import Decimal as D
import sqlite3
import pytest
from app.paper_ledger import PaperLedger


def order(ledger, **kwargs):
    values = dict(key='buy', symbol='005930', name='Samsung', side='BUY', quantity=10, price=D('100'), observed_at='2026-10-27T09:05:00+09:00', received_at='2026-10-27T09:05:01+09:00', reason='test', fee_rate=D('0.001'), tax_rate=D('0.002'), at='2026-10-27T09:05:01+09:00')
    values.update(kwargs)
    return ledger.execute(**values)


def test_buy_partial_full_sell_restart(tmp_path):
    path = tmp_path/'paper.sqlite'
    ledger = PaperLedger(path, D('10000'))
    assert order(ledger)
    state = ledger.snapshot()
    assert D(state['cash']) == D('8999')
    assert D(state['positions'][0]['average_price']) == D('100.1')
    ledger.mark('005930', D('130'))
    ledger.mark('005930', D('120'))
    assert order(ledger,key='partial',side='SELL',quantity=4,price=D('120'),partial=True)
    state = ledger.snapshot()
    assert state['positions'][0]['quantity']==6
    assert state['positions'][0]['partial_taken'] is False
    assert D(state['positions'][0]['high_price']) == 130
    assert D(state['realized_profit']) == D('78.16')
    ledger = PaperLedger(path, D('999'))
    assert D(ledger.snapshot()['initial_cash']) == 10000
    assert order(ledger,key='final',side='SELL',quantity=6,price=D('110'))
    assert ledger.snapshot()['positions']==[]
    assert D(ledger.snapshot()['realized_profit'])==D('135.58')
    ledger.reconcile()


def test_idempotency_and_cash_reservation(tmp_path):
    ledger = PaperLedger(tmp_path/'paper.sqlite',D('1001'))
    assert order(ledger)
    before = ledger.snapshot()
    assert order(ledger) is False
    assert ledger.snapshot() == before
    with pytest.raises(ValueError,match='cash'):
        order(ledger,key='another')
    assert ledger.snapshot() == before
    with pytest.raises(ValueError,match='position'):
        order(ledger,key='oversell',side='SELL',quantity=11)
    assert ledger.snapshot() == before


@pytest.mark.parametrize('changes', [dict(quantity=0),dict(quantity=True),dict(price=D('NaN')),dict(price=D('-1')),dict(fee_rate=D('-0.1')),dict(tax_rate=D('1')),dict(side='INVALID')])
def test_invalid_order_rolls_back(tmp_path,changes):
    ledger = PaperLedger(tmp_path/'paper.sqlite')
    before = ledger.snapshot()
    with pytest.raises(ValueError):
        order(ledger,**changes)
    assert ledger.snapshot() == before


@pytest.mark.parametrize('sql', ["UPDATE state SET cash='1'", "UPDATE positions SET quantity=3", "UPDATE fills SET cash_delta='0'"])
def test_corruption_fails_closed_on_restart(tmp_path, sql):
    path = tmp_path/'paper.sqlite'
    ledger = PaperLedger(path)
    order(ledger)
    with sqlite3.connect(path) as db:
        db.execute(sql)
    with pytest.raises(ValueError,match='reconciliation'):
        PaperLedger(path)


def test_persistent_metadata_reports_and_deduplicated_events(tmp_path):
    path = tmp_path/'paper.sqlite'
    ledger = PaperLedger(path)
    ledger.set_meta('week',{'start':'2026-10-27'})
    ledger.event('paused','stale quote','09:00')
    ledger.event('paused','stale quote','09:01')
    ledger.save_report('2026-10-27',{'fills':1})
    ledger.save_report('2026-10-27',{'fills':2})
    ledger = PaperLedger(path)
    assert ledger.get_meta('week') == {'start':'2026-10-27'}
    assert ledger.get_meta('absent',False) is False
    assert len(ledger.snapshot()['events'])==1
    assert ledger.snapshot()['reports']==[{'day':'2026-10-27','report':{'fills':2}}]


def test_sqlite_failure_rolls_back_cash_and_position(tmp_path):
    path = tmp_path/'paper.sqlite'
    ledger = PaperLedger(path)
    before = ledger.snapshot()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TRIGGER fail_fill BEFORE INSERT ON fills BEGIN SELECT RAISE(ABORT,'forced failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        order(ledger)
    assert ledger.snapshot()==before


def test_all_fill_statistics_survive_recent_limit_and_restart(tmp_path):
    path = tmp_path/'paper.sqlite'
    ledger = PaperLedger(path)
    for index in range(55):
        order(ledger,key=f'b{index}',quantity=1,price=D('100'),fee_rate=D('0'),tax_rate=D('0'))
        order(ledger,key=f's{index}',side='SELL',quantity=1,price=D('90') if index < 54 else D('120'),fee_rate=D('0'),tax_rate=D('0'))
    ledger = PaperLedger(path)
    assert ledger.get_meta('schema_version') == '1'
    assert len(ledger.snapshot()['fills']) == 100
    totals = ledger.snapshot()['totals']
    assert totals['fill_count'] == 110
    assert totals['buy_count'] == totals['sell_count'] == 55
    assert totals['winning_exits'] == 1
    assert totals['losing_exits'] == 54
    assert totals['max_consecutive_losses'] == 54
    assert D(totals['avg_gain']) == 20
    assert D(totals['avg_loss']) == -10
    assert D(totals['profit_factor']) == D(20) / D(540)
    stats = ledger.daily_stats('2026-10-27')
    assert stats['entry_count'] == stats['exit_count'] == 55
    assert stats['consecutive_losses'] == 0
    assert D(stats['realized_profit']) == -520
    assert ledger.daily_stats('2026-10-28')['entry_count'] == 0


def test_daily_loss_streak_and_costs(tmp_path):
    ledger = PaperLedger(tmp_path/'paper.sqlite')
    order(ledger)
    order(ledger,key='loss',side='SELL',quantity=1,price=D('90'))
    stats = ledger.daily_stats('2026-10-27')
    assert stats['consecutive_losses']==1
    assert D(stats['fees'])==D('1.09')
    assert D(stats['taxes'])==D('0.18')
    assert D(stats['realized_profit'])==D('-10.37')
    assert ledger.daily_stats('2026-10-28')['consecutive_losses']==0


@pytest.mark.parametrize('sql', ["UPDATE positions SET cost_basis='1'", "UPDATE positions SET average_price='1'", "UPDATE fills SET realized_profit='0' WHERE side='SELL'"])
def test_cost_and_per_sell_profit_reconciled(tmp_path, sql):
    path = tmp_path/'paper.sqlite'
    ledger = PaperLedger(path)
    order(ledger)
    order(ledger,key='sell',side='SELL',quantity=1,price=D('120'))
    with sqlite3.connect(path) as db:
        db.execute(sql)
    with pytest.raises(ValueError,match='reconciliation'):
        PaperLedger(path)


def test_unsupported_version_does_not_create_or_change_schema(tmp_path):
    path = tmp_path/'future.sqlite'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO meta VALUES(?,?)', ('schema_version','"99"'))
    before = path.read_bytes()
    with pytest.raises(ValueError,match='Unsupported'):
        PaperLedger(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as db:
        assert [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]==['meta']


def test_partial_target_survives_small_fills_and_restart(tmp_path):
    path = tmp_path/'paper.sqlite'
    ledger = PaperLedger(path)
    order(ledger)
    order(ledger,key='p1',side='SELL',quantity=1,partial=True)
    ledger = PaperLedger(path)
    position = ledger.snapshot()['positions'][0]
    assert (position['partial_target'],position['partial_filled'],position['partial_taken'])==(5,1,False)
    order(ledger,key='p2',side='SELL',quantity=3,partial=True)
    position=ledger.snapshot()['positions'][0]
    assert (position['partial_target'],position['partial_filled'],position['partial_taken'])==(5,4,False)
    order(ledger,key='p3',side='SELL',quantity=1,partial=True)
    position=ledger.snapshot()['positions'][0]
    assert (position['partial_target'],position['partial_filled'],position['partial_taken'])==(5,5,True)
