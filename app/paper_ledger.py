"""Durable, isolated paper money ledger. Never submits broker orders."""
from decimal import Decimal
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3


def money(value):
    value = Decimal(value)
    if not value.is_finite():
        raise ValueError("Non-finite money")
    return value


class PaperLedger:
    def __init__(self, path: Path, initial_cash=Decimal('10000000')):
        self.path = Path(path)
        # Read existing version without WAL/schema writes before accepting the file.
        if self.path.exists():
            check = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True)
            try:
                if check.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
                    version = check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                    if version and json.loads(version[0]) != '1':
                        raise ValueError('Unsupported paper ledger schema')
            finally:
                check.close()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        initial_cash = money(initial_cash)
        if initial_cash < 0:
            raise ValueError('Negative initial cash')
        with self._connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS state(id INTEGER PRIMARY KEY CHECK(id=1), initial_cash TEXT NOT NULL,cash TEXT NOT NULL,realized_profit TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS positions(symbol TEXT PRIMARY KEY,name TEXT NOT NULL,quantity INTEGER NOT NULL CHECK(quantity>0),cost_basis TEXT NOT NULL,average_price TEXT NOT NULL,high_price TEXT NOT NULL,partial_taken INTEGER NOT NULL,opened_at TEXT NOT NULL,last_price TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS fills(id INTEGER PRIMARY KEY,key TEXT NOT NULL UNIQUE,symbol TEXT NOT NULL,name TEXT NOT NULL,side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),quantity INTEGER NOT NULL CHECK(quantity>0),price TEXT NOT NULL,gross TEXT NOT NULL,fee TEXT NOT NULL,tax TEXT NOT NULL,cash_delta TEXT NOT NULL,realized_profit TEXT NOT NULL,observed_at TEXT NOT NULL,received_at TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,message TEXT NOT NULL,at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS reports(day TEXT PRIMARY KEY,report TEXT NOT NULL);
            ''')
            columns = {r['name'] for r in db.execute('PRAGMA table_info(positions)')}
            for field in ('partial_target', 'partial_filled'):
                if field not in columns:
                    db.execute(f'ALTER TABLE positions ADD COLUMN {field} INTEGER NOT NULL DEFAULT 0')
            db.execute('INSERT OR IGNORE INTO state VALUES(1,?,?,?)', (str(initial_cash), str(initial_cash), '0'))
        if self.get_meta("schema_version") not in (None, "1"):
            raise ValueError("Unsupported paper ledger schema")
        self.set_meta("schema_version", "1")
        self.reconcile()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def set_meta(self, key, value):
        with self._connect() as db:
            db.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def get_meta(self, key, default=None):
        with self._connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def event(self, kind, message, at):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT kind,message FROM events ORDER BY id DESC LIMIT 1').fetchone()
            if previous and tuple(previous) == (kind, message):
                return
            db.execute('INSERT INTO events(kind,message,at) VALUES(?,?,?)', (kind, message, at))

    def mark(self, symbol, price):
        price = money(price)
        if price <= 0:
            raise ValueError('Nonpositive price')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT high_price FROM positions WHERE symbol=?', (symbol,)).fetchone()
            if row:
                db.execute('UPDATE positions SET high_price=?,last_price=? WHERE symbol=?', (str(max(price, money(row[0]))), str(price), symbol))

    def execute(self, *, key, symbol, name, side, quantity, price, observed_at, received_at, reason, fee_rate, tax_rate, at, partial=False):
        price, fee_rate, tax_rate = map(money, (price, fee_rate, tax_rate))
        side = side.upper()
        if not key or not symbol or side not in ('BUY', 'SELL') or isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0 or price <= 0 or not 0 <= fee_rate < 1 or not 0 <= tax_rate < 1:
            raise ValueError('Invalid paper order')
        gross = price * quantity
        fee = gross * fee_rate
        tax = gross * tax_rate if side == 'SELL' else Decimal(0)
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM fills WHERE key=?', (key,)).fetchone():
                return False
            state = db.execute('SELECT * FROM state WHERE id=1').fetchone()
            old = db.execute('SELECT * FROM positions WHERE symbol=?', (symbol,)).fetchone()
            cash, realized = money(state['cash']), Decimal(0)
            if side == 'BUY':
                delta = -(gross + fee)
                if cash + delta < 0:
                    raise ValueError('Insufficient paper cash')
                qty = (old['quantity'] if old else 0) + quantity
                cost = (money(old['cost_basis']) if old else Decimal(0)) + gross + fee
                high = max(price, money(old['high_price'])) if old else price
                db.execute('INSERT INTO positions(symbol,name,quantity,cost_basis,average_price,high_price,partial_taken,opened_at,last_price) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET name=excluded.name,quantity=excluded.quantity,cost_basis=excluded.cost_basis,average_price=excluded.average_price,high_price=excluded.high_price,last_price=excluded.last_price', (symbol,name,qty,str(cost),str(cost/qty),str(high),old['partial_taken'] if old else 0,old['opened_at'] if old else at,str(price)))
            else:
                if not old or quantity > old['quantity']:
                    raise ValueError('Insufficient paper position')
                delta = gross - fee - tax
                cost = money(old['cost_basis'])
                sold_cost = cost if quantity == old['quantity'] else cost * quantity / old['quantity']
                realized = delta - sold_cost
                qty = old['quantity'] - quantity
                if qty:
                    remaining = cost - sold_cost
                    target = old['partial_target'] or max(1, old['quantity'] // 2) if partial else old['partial_target']
                    filled = old['partial_filled'] + quantity if partial else old['partial_filled']
                    taken = old['partial_taken'] or bool(target and filled >= target)
                    db.execute('UPDATE positions SET quantity=?,cost_basis=?,average_price=?,last_price=?,high_price=?,partial_taken=?,partial_target=?,partial_filled=? WHERE symbol=?', (qty,str(remaining),str(remaining/qty),str(price),str(max(price,money(old['high_price']))),int(taken),target,filled,symbol))
                else:
                    db.execute('DELETE FROM positions WHERE symbol=?', (symbol,))
            db.execute('UPDATE state SET cash=?,realized_profit=? WHERE id=1', (str(cash+delta),str(money(state['realized_profit'])+realized)))
            db.execute('INSERT INTO fills(key,symbol,name,side,quantity,price,gross,fee,tax,cash_delta,realized_profit,observed_at,received_at,reason,at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (key,symbol,name,side,quantity,str(price),str(gross),str(fee),str(tax),str(delta),str(realized),observed_at,received_at,reason,at))
        return True

    def reconcile(self):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT * FROM state WHERE id=1').fetchone()
            expected = money(state['initial_cash'])
            quantities, costs = {}, {}
            realized = Decimal(0)
            for row in db.execute('SELECT * FROM fills ORDER BY id'):
                symbol, quantity = row['symbol'], row['quantity']
                gross = money(row['price']) * quantity
                if quantity <= 0 or gross <= 0 or money(row['fee']) < 0 or money(row['tax']) < 0:
                    raise ValueError('Paper fill reconciliation failed')
                previous_qty = quantities.get(symbol, 0)
                previous_cost = costs.get(symbol, Decimal(0))
                if row['side'] == 'BUY':
                    delta = -(gross + money(row['fee']))
                    trade_profit = Decimal(0)
                    quantities[symbol] = previous_qty + quantity
                    costs[symbol] = previous_cost - delta
                elif row['side'] == 'SELL' and quantity <= previous_qty:
                    delta = gross - money(row['fee']) - money(row['tax'])
                    sold_cost = previous_cost if quantity == previous_qty else previous_cost * quantity / previous_qty
                    trade_profit = delta - sold_cost
                    quantities[symbol] = previous_qty - quantity
                    costs[symbol] = previous_cost - sold_cost
                else:
                    raise ValueError('Paper quantity reconciliation failed')
                if gross != money(row['gross']) or delta != money(row['cash_delta']) or trade_profit != money(row['realized_profit']):
                    raise ValueError('Paper fill reconciliation failed')
                expected += delta
                realized += trade_profit
                if expected < 0:
                    raise ValueError('Paper history reconciliation failed')
            positions = list(db.execute('SELECT * FROM positions'))
            actual = {r['symbol']:r['quantity'] for r in positions}
            if expected != money(state['cash']) or realized != money(state['realized_profit']) or actual != {s:q for s,q in quantities.items() if q}:
                raise ValueError('Paper ledger reconciliation failed; execution disabled')
            for row in positions:
                cost = costs[row['symbol']]
                if money(row['cost_basis']) != cost or money(row['average_price']) != cost / row['quantity']:
                    raise ValueError('Paper cost reconciliation failed; execution disabled')

    def save_report(self, day, reportdict):
        with self._connect() as db:
            db.execute('INSERT INTO reports VALUES(?,?) ON CONFLICT(day) DO UPDATE SET report=excluded.report', (day,json.dumps(reportdict,ensure_ascii=False,allow_nan=False)))

    def snapshot(self):
        with self._connect() as db:
            db.execute('BEGIN')
            state = dict(db.execute('SELECT initial_cash,cash,realized_profit FROM state WHERE id=1').fetchone())
            state['positions'] = [dict(r) for r in db.execute('SELECT * FROM positions ORDER BY symbol')]
            for position in state['positions']:
                position['partial_taken'] = bool(position['partial_taken'])
            state['totals'] = self._totals(list(db.execute('SELECT * FROM fills ORDER BY id')))
            state['fills'] = [dict(r) for r in db.execute('SELECT * FROM fills ORDER BY id DESC LIMIT 100')]
            state['events'] = [dict(r) for r in db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 100')]
            state['reports'] = [dict(day=r['day'], report=json.loads(r['report'])) for r in db.execute('SELECT * FROM reports ORDER BY day DESC')]
            return state

    def daily_stats(self, day: str):
        """Count committed trades from the complete ledger, including after restart."""
        with self._connect() as db:
            rows = list(db.execute('SELECT * FROM fills WHERE substr(at,1,10)=? ORDER BY id', (day,)))
        exits = [r for r in rows if r['side'] == 'SELL']
        streak = 0
        for row in exits:
            streak = streak + 1 if money(row['realized_profit']) < 0 else 0
        return {
            'entry_count': sum(r['side'] == 'BUY' for r in rows),
            'exit_count': len(exits),
            'fees': str(sum((money(r['fee']) for r in rows), Decimal(0))),
            'taxes': str(sum((money(r['tax']) for r in rows), Decimal(0))),
            'realized_profit': str(sum((money(r['realized_profit']) for r in exits), Decimal(0))),
            'consecutive_losses': streak,
        }

    @staticmethod
    def _totals(rows):
        exits = [money(r['realized_profit']) for r in rows if r['side'] == 'SELL']
        gains = [value for value in exits if value > 0]
        losses = [value for value in exits if value < 0]
        gain = sum(gains, Decimal(0))
        loss = -sum(losses, Decimal(0))
        streak = maximum = 0
        for value in exits:
            streak = streak + 1 if value < 0 else 0
            maximum = max(maximum, streak)
        return {
            'fill_count': len(rows),
            'buy_count': sum(r['side'] == 'BUY' for r in rows),
            'sell_count': len(exits),
            'total_fees': str(sum((money(r['fee']) for r in rows), Decimal(0))),
            'total_taxes': str(sum((money(r['tax']) for r in rows), Decimal(0))),
            'winning_exits': len(gains), 'losing_exits': len(losses),
            'profit_factor': str(gain / loss) if loss else None,
            'avg_gain': str(gain / len(gains)) if gains else '0',
            'avg_loss': str(-loss / len(losses)) if losses else '0',
            'expectancy': str(sum(exits, Decimal(0)) / len(exits)) if exits else '0',
            'max_consecutive_losses': maximum,
        }
