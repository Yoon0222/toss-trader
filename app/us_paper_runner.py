"""US-only forward validation: read-only quotes, isolated virtual USD ledger."""
import asyncio
import contextlib
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from zoneinfo import ZoneInfo
from app.paper_ledger import PaperLedger
from app.paper_runner import WorkerLock, KST, parse_time, finite, fresh, executable_quote

NY=ZoneInfo('America/New_York')
D=Decimal


def us_session_window(calendar, now):
    today=calendar['today']
    session_date=now.astimezone(NY).date()
    if today['date']!=session_date.isoformat():raise ValueError('calendar date mismatch')
    regular=today.get('regularMarket')
    if not regular:return None
    opened=parse_time(regular['startTime']).astimezone(NY)
    closed=parse_time(regular['endTime']).astimezone(NY)
    if closed<=opened or opened.date()!=session_date or closed.date()!=session_date:raise ValueError('invalid US session')
    return {'open':opened,'buy_start':opened+timedelta(minutes=5),'liquidate':closed-timedelta(minutes=30),'continuous_end':closed,'close':closed}


def us_breakout_signal(candles,now,window):
    now=now.astimezone(NY)
    if any(c.get('currency')!='USD' for c in candles):return None
    rows=sorted([c for c in candles if c.get('currency')=='USD' and window['open']<=parse_time(c['timestamp'])-timedelta(minutes=1) and parse_time(c['timestamp'])<=min(now,window['close']) and parse_time(c['timestamp']).astimezone(NY).date()==now.date()],key=lambda c:parse_time(c['timestamp']))
    if len(rows)<22:return None
    if len({parse_time(c['timestamp']) for c in rows})!=len(rows):return None
    current=rows[-1];previous=rows[-21:-1]
    if not fresh(current['timestamp'],now,180):return None
    volume=finite(current['volume']);avg=sum((finite(c['volume']) for c in previous),D(0))/20
    close=finite(current['closePrice']);prior_high=max(finite(c['highPrice']) for c in previous)
    total=sum((finite(c['volume']) for c in rows),D(0))
    if total<=0 or avg<=0:return None
    vwap=sum(((finite(c['highPrice'])+finite(c['lowPrice'])+finite(c['closePrice']))/3*finite(c['volume']) for c in rows),D(0))/total
    if volume>=max(avg*2,D(10000)) and close>prior_high and close>vwap and close>finite(current['openPrice']):
        return {'candle_at':current['timestamp'],'close':str(close),'volume_ratio':str(volume/avg),'vwap':str(vwap)}
    return None

class USPaperValidation:
    def __init__(self,market,settings,clock=None):
        self.market=market;self.settings=settings
        self.clock=clock or (lambda: datetime.now(NY))
        self.path=Path(settings.us_paper_data_dir)
        self.lock=WorkerLock(self.path/"worker.lock")
        self.ledger=None;self.task=None;self.phase="disabled";self.last_attempt=None;self.last_success=None;self.error=None
    async def start(self):
        if not self.settings.us_paper_enabled:return
        if not self.lock.acquire():self.phase="locked";self.error="다른 모의검증 프로세스가 실행 중이에요.";return
        try:
            self.ledger=PaperLedger(self.path/"ledger.sqlite3",D("10000"))
            state=self.ledger.snapshot()
            if finite(state['initial_cash'])!=D('10000'):raise ValueError('invalid_us_capital')
            stored=self.ledger.get_meta('experiment')
            if not stored and (state['totals']['fill_count'] or state['positions'] or state['events'] or state['reports'] or finite(state['cash'])!=D('10000')):
                raise ValueError('orphan_nonempty_ledger')
            if self.ledger.get_meta('market') not in (None,'US') or self.ledger.get_meta('currency') not in (None,'USD'):
                raise ValueError('ledger_currency_mismatch')
            # Never reset an existing experiment automatically.
            configured={"start":str(self.settings.us_paper_start_date),"end":str(self.settings.us_paper_end_date),"currency":"USD","timezone":"America/New_York","initial_cash":"10000","per_symbol":"equity_fraction","position_fraction":"0.5","max_positions":3,"daily_loss":"200","fee_rate":"0.001","sell_tax_assumption":"0","slippage":"0.0005"}
            stored=self.ledger.get_meta("experiment")
            legacy_configured = dict(configured, per_symbol="1000")
            legacy_configured.pop("position_fraction")
            previous_configured = json.loads(stored) if stored else None
            capital_upgraded = previous_configured == legacy_configured
            if previous_configured and previous_configured not in (configured, legacy_configured):
                raise ValueError("experiment_config_changed")
            strategy={"version":"us-volume-breakout-v4","universe":"MARKET_TRADING_AMOUNT/realtime/top100","change_min":"3","change_max":"10","turnover_min":"10000000","volume_multiple":"2","minute_volume_min":"10000","breakout_lookback":20,"candle_limit":60,"quote_max_age_seconds":15,"signal_max_age_seconds":180,"stop":"-1%","partial":"+3%/50%","trailing":"-2%","max_hold_minutes":None,"daily_entries":5,"position_fraction":"0.5","max_consecutive_losses":None,"exclude_losing_symbols_same_day":True}
            saved=self.ledger.get_meta("strategy")
            previous_strategy = json.loads(saved) if saved else None
            timed_strategy = dict(strategy, version="us-volume-breakout-v3", max_hold_minutes=90)
            legacy_v2 = dict(timed_strategy, version="us-volume-breakout-v2")
            legacy_v2.pop("position_fraction")
            legacy_strategy = dict(legacy_v2, version="us-volume-breakout-v1", max_consecutive_losses=3)
            legacy_strategy.pop("exclude_losing_symbols_same_day")
            upgraded = previous_strategy == legacy_strategy
            if previous_strategy and previous_strategy not in (strategy, timed_strategy, legacy_v2, legacy_strategy):
                raise ValueError("strategy_changed")
            self.ledger.set_meta('market','US');self.ledger.set_meta('currency','USD')
            self.ledger.set_meta('experiment',json.dumps(configured))
            # Commit the policy version and its single audit event together.
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("strategy",json.dumps(json.dumps(strategy))))
                if previous_strategy and previous_strategy != strategy:
                    db.execute("INSERT INTO events(kind,message,at) VALUES(?,?,?)", ("max_hold_removed","90분 보유 강제매도 제거. 손절·트레일링·분할익절·장마감 청산 유지.",self.clock().isoformat()))
            if upgraded:
                self.event("strategy_policy_changed", "미국 모의검증 v2: 연속손실 중단 해제, 당일 손실매도 종목 재진입 제외. 일손실 200 USD·일진입 5회 유지.", self.clock().astimezone(NY))
            if capital_upgraded:
                self.event("position_sizing_changed", "종목당 진입 한도를 현재 가상 순자산의 50%로 변경. 수수료 포함·현금 한도 적용, 기존 체결과 잔고 보존.", self.clock())
            self.phase="scheduled";self.task=asyncio.create_task(self.run(),name="us-paper-validation")
        except Exception:
            self.phase="error";self.error="가상 원장 복구 또는 검증 설정을 확인해 주세요.";self.ledger=None;self.lock.close()
    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):await self.task
        self.lock.close()
    def event(self,kind,message,now):
        self.ledger.event(kind,message,now.isoformat())
    def equity(self,snapshot):
        return finite(snapshot["cash"])+sum((finite(p["last_price"])*p["quantity"] for p in snapshot["positions"]),D(0))
    def entry_budget(self, state):
        return max(D(0), min(self.equity(state)*D("0.5"), finite(state["cash"])))

    def snapshot(self):
        state=self.ledger.snapshot() if self.ledger else None
        return {"enabled":self.settings.us_paper_enabled,"phase":self.phase,"error":self.error,"last_attempt":self.last_attempt,"last_success":self.last_success,"start_date":str(self.settings.us_paper_start_date),"end_date":str(self.settings.us_paper_end_date),"market":"US","currency":"USD","timezone":"America/New_York","real_orders_available":False,"paper_account":state,"excluded_symbols":sorted(self.excluded_symbols(self.clock().astimezone(NY).date())) if self.ledger else [],"last_scan":json.loads(self.ledger.get_meta("last_scan","null")) if self.ledger else None,"entries_paused":bool(self.ledger and self.ledger.get_meta("paused","false")=="true"),"equity":str(self.equity(state)) if state else None,"rules":{"currency":"USD","timezone":"America/New_York","initial_cash":"10000","per_symbol":str(self.entry_budget(state)) if state else None,"position_fraction":"0.5","sizing_basis":"cash_plus_last_marked_positions","max_positions":3,"daily_loss_limit":"200","daily_entry_limit":5,"max_consecutive_losses":None,"consecutive_loss_stop":False,"exclude_losing_symbols_same_day":True,"max_hold_minutes":None,"stop_loss":"-1%","partial_take":"+3% 시 50%","trailing":"-2%","fees":"편도 0.1% 가정","sell_tax_assumption":"0% 가정","slippage":"편도 0.05%"}}
    def report(self,day,now,final=False):
        state=self.ledger.snapshot();equity=self.equity(state)
        peak=finite(self.ledger.get_meta("peak_equity","10000"));peak=max(peak,equity)
        drawdown=(equity/peak-1)*100 if peak else D(0)
        worst=min(finite(self.ledger.get_meta("mdd","0")),drawdown)
        self.ledger.set_meta("peak_equity",str(peak));self.ledger.set_meta("mdd",str(worst))
        report={"day":day,"market":"US","currency":"USD","timezone":"America/New_York","generated_at":now.isoformat(),"final":final,"cash":state["cash"],"equity_marked":str(equity),"net_profit_marked":str(equity-D("10000")),"return_pct":str((equity/D("10000")-1)*100),"realized_profit":state["realized_profit"],"open_positions":len(state["positions"]),"unliquidated":bool(state["positions"]),"max_drawdown_observed_pct":str(worst),"valuation_note":"마지막 정상 호가 기준 가상평가. 미청산 수익은 확정 아님.","totals":state["totals"],"daily":self.ledger.daily_stats(day),"cost_model":"USD 수수료 편도0.1%, 매도세금가정0%, 슬리피지 편도0.05%; 실제 부과율 아님"}
        self.ledger.save_report(day,report)
        report_dir=self.path/"reports";report_dir.mkdir(exist_ok=True)
        target=report_dir/(day+("-final" if final else "")+".json");temp=target.with_suffix(".tmp")
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8");temp.replace(target)
        return report
    async def run(self):
        while True:
            try:await asyncio.wait_for(self.tick(self.clock().astimezone(NY)),timeout=25)
            except asyncio.CancelledError:raise
            except Exception:
                self.phase="error";self.error="모의검증 처리 실패. 신규 진입을 보류했어요. 연결·가상 원장을 확인해 주세요."
                try:self.event("error",self.error,self.clock().astimezone(NY))
                except Exception:pass
            await asyncio.sleep(10)
    def excluded_symbols(self, day):
        # Read the full committed history, not snapshot's most recent 100 fills.
        with self.ledger._connect() as db:
            rows = db.execute("SELECT symbol,reason,realized_profit,at FROM fills WHERE side='SELL'").fetchall()
        return {row['symbol'] for row in rows if parse_time(row['at']).astimezone(NY).date().isoformat() == str(day) and (row['reason'] == 'stop_loss' or finite(row['realized_profit']) < 0)}

    async def tick(self,now):
        now=now.astimezone(NY);self.last_attempt=now.isoformat()
        day=now.date();start=self.settings.us_paper_start_date;end=self.settings.us_paper_end_date
        if day<start:self.phase="scheduled";return
        if day>end:
            self.phase="completed_with_positions" if self.ledger.snapshot()["positions"] else "completed"
            if not self.ledger.get_meta("final_report"):
                self.report(str(end),now,True);self.ledger.set_meta("final_report","done")
            return

        if self.ledger.get_meta("closed:"+str(day)):
            self.phase="unliquidated" if self.ledger.snapshot()["positions"] else ("completed" if day==end else "daily_complete")
            return
        calendar=await self.market.get("/api/v1/market-calendar/US",{"date":str(day)},ttl=300)
        window=us_session_window(calendar,now)
        if not window:self.phase="holiday";self.last_success=now.isoformat();return
        if now<window["open"]:self.phase="waiting_market";self.last_success=now.isoformat();return
        if now>=window["close"]:
            self.report(str(day),now,day==end);self.ledger.set_meta("closed:"+str(day),"done")
            self.phase="unliquidated" if self.ledger.snapshot()["positions"] else ("completed" if day==end else "daily_complete")
            if day==end:self.ledger.set_meta("final_report","done")
            return
        self.error=None;self.phase="running"
        if self.ledger.get_meta("day_start:"+str(day)) is None:self.ledger.set_meta("day_start:"+str(day),str(self.equity(self.ledger.snapshot())))
        paused=self.ledger.get_meta("paused","false")=="true"
        # Manage exits before evaluating new entries. Pause means no new entries; protective exits continue.
        for position in self.ledger.snapshot()["positions"]:
            await self.manage_position(position,now,window)
        state=self.ledger.snapshot();equity=self.equity(state)
        peak=max(finite(self.ledger.get_meta("peak_equity","10000")),equity)
        self.ledger.set_meta("peak_equity",str(peak))
        self.ledger.set_meta("mdd",str(min(finite(self.ledger.get_meta("mdd","0")),(equity/peak-1)*100)))
        baseline=finite(self.ledger.get_meta("day_start:"+str(day)))
        loss=equity-baseline
        if loss<=-D("200"):self.ledger.set_meta("loss_stop:"+str(day),"true")
        halted=self.ledger.get_meta("loss_stop:"+str(day))=="true"
        if self.error:
            self.phase="degraded";return
        if paused or halted or now<window["buy_start"] or now>=window["liquidate"] or now>=window["continuous_end"]:
            self.phase="paused_entries" if paused else "loss_limit" if halted else "liquidating" if now>=window["liquidate"] else "waiting_entry"
            self.last_success=now.isoformat();return
        if self.error:
            self.phase="degraded";return
        if len(state["positions"])>=3:self.last_success=now.isoformat();return
        if self.ledger.daily_stats(str(day))["entry_count"]>=5:self.phase="daily_entry_limit";return
        scan_deadline=time.monotonic()+8
        ranking=await asyncio.wait_for(self.market.rankings(ranking_type="MARKET_TRADING_AMOUNT",duration="realtime",count=100,market_country="US"),timeout=8)
        if not ranking["observed_at"] or not fresh(ranking["observed_at"],self.clock().astimezone(NY),120):raise ValueError("ranking_stale")
        stocks=ranking['stocks']
        excluded = self.excluded_symbols(day)
        cursor=int(self.ledger.get_meta('scan_cursor',0))%len(stocks) if stocks else 0
        for offset in range(len(stocks)):
            at=self.clock().astimezone(NY)
            if time.monotonic()>=scan_deadline or at.date()!=day or not window['buy_start']<=at<window['liquidate']:break
            index=(cursor+offset)%len(stocks)
            stock=stocks[index]
            self.ledger.set_meta('scan_cursor',(index+1)%len(stocks))
            state=self.ledger.snapshot()
            if len(state["positions"])>=3:break
            if self.ledger.daily_stats(str(day))["entry_count"]>=5:break
            if stock["code"] in excluded:continue
            if stock["market"] not in {"NASDAQ","NYSE","AMEX"}:continue
            if any(p["symbol"]==stock["code"] for p in state["positions"]):continue
            if stock.get("security_type") != "STOCK":continue
            if stock.get("currency") != "USD" or finite(stock.get("price") or 0)<D("5"):continue
            if stock["change"] is None or not D("3")<=finite(stock["change"])<=D("10"):continue
            if finite(stock.get("turnover") or 0)<D("10000000"):continue
            remaining=scan_deadline-time.monotonic()
            if remaining<=0:break
            try:
                candles=await asyncio.wait_for(self.market.get("/api/v1/candles",{"symbol":stock["code"],"interval":"1m","count":60,"adjusted":False},ttl=15),timeout=remaining)
            except asyncio.TimeoutError:break
            signal=us_breakout_signal(candles["candles"],self.clock().astimezone(NY),window)
            if not signal:continue
            # Ensure session did not expire while REST requests were running.
            at=self.clock().astimezone(NY)
            if at.date()!=day or at>=window["liquidate"]:break
            remaining=scan_deadline-time.monotonic()
            if remaining<=0:break
            try:
                book=await asyncio.wait_for(self.market.get("/api/v1/orderbook",{"symbol":stock["code"]},ttl=0),timeout=remaining)
            except asyncio.TimeoutError:break
            at=self.clock().astimezone(NY)
            if not (self.settings.us_paper_start_date<=at.date()<=self.settings.us_paper_end_date and at.date()==day and window["buy_start"]<=at<window["liquidate"] and at<window["continuous_end"]):break
            if not fresh(signal["candle_at"],at,180):continue
            ask=min(finite(r["price"]) for r in book["asks"])
            if ask<D("5") or not D("0.99")<=ask/finite(signal["close"])<=D("1.01"):continue
            budget=self.entry_budget(state)
            wanted=int((budget/(ask*D("1.0005")*D("1.001"))).to_integral_value(rounding=ROUND_DOWN))
            if wanted<=0:continue
            try:qty,price=executable_quote(book,"BUY",wanted,self.clock().astimezone(NY),currency="USD")
            except ValueError:
                self.event("entry_skipped","호가 신선도·유동성 조건 미충족: "+stock["code"],at);continue
            qty=min(qty,int((budget/(price*D("1.001"))).to_integral_value(rounding=ROUND_DOWN)))
            if qty<=0:continue
            at=self.clock().astimezone(NY)
            if not (window["buy_start"]<=at<window["liquidate"] and at<window["continuous_end"] and at.date()==day and self.settings.us_paper_start_date<=at.date()<=self.settings.us_paper_end_date):break
            if time.monotonic()>=scan_deadline or self.ledger.get_meta("paused","false")=="true" or not fresh(ranking["observed_at"],at,120):break
            key=f"entry:{day}:{stock['code']}:{signal['candle_at']}"
            if self.ledger.execute(key=key,symbol=stock["code"],name=stock["name"],side="BUY",quantity=qty,price=price,observed_at=book["timestamp"],received_at=self.clock().astimezone(NY).isoformat(),reason="volume_breakout",fee_rate=D("0.001"),tax_rate=D(0),at=at.isoformat()):
                self.event("paper_buy","실제 호가 기반 가상 매수: "+stock["code"],at)
        self.ledger.set_meta("last_scan",json.dumps({"at":self.clock().astimezone(NY).isoformat(),"universe_count":len(ranking["stocks"]),"strategy":"us-volume-breakout-v4"}))
        self.last_success=self.clock().astimezone(NY).isoformat()
    async def manage_position(self,position,now,window):
        try:
            book=await self.market.get("/api/v1/orderbook",{"symbol":position["symbol"]},ttl=0)
            at=self.clock().astimezone(NY)
            if not (window["open"]<=at<window["continuous_end"]) or at.date()!=now.astimezone(NY).date():raise ValueError("outside_regular_session")
            qty,bid=executable_quote(book,"SELL",position["quantity"],at,currency="USD")
            self.ledger.mark(position["symbol"],bid)
            average=finite(position["average_price"]);high=max(finite(position["high_price"]),bid)
            overnight=parse_time(position["opened_at"]).astimezone(NY).date()<at.date()
            reason=None;partial=False
            if overnight or at>=window["liquidate"]:reason="end_of_day"
            elif bid<=average*D("0.99"):reason="stop_loss"
            elif bid<=high*D("0.98"):reason="trailing_stop"
            elif not position["partial_taken"] and bid>=average*D("1.03"):
                reason="partial_take";partial=True
                target=position.get("partial_target",0) or max(1,position["quantity"]//2)
                remaining=target-position.get("partial_filled",0)
                qty=min(qty,remaining)
            if not reason:return
            key=f"exit:{position['symbol']}:{book['timestamp']}:{reason}"
            self.ledger.execute(key=key,symbol=position["symbol"],name=position["name"],side="SELL",quantity=qty,price=bid,observed_at=book["timestamp"],received_at=at.isoformat(),reason=reason,fee_rate=D("0.001"),tax_rate=D("0"),at=at.isoformat(),partial=partial)
        except Exception:
            self.error="가상 보유 종목의 호가·청산 확인 실패. 신규 매수 보류, 다음 주기에 재확인해요."
            self.event("exit_deferred",self.error+" "+position["symbol"],now)
    def pause(self):
        if not self.ledger:return {"status":"unavailable","message":"가상 원장이 준비되지 않았어요."}
        if self.ledger:self.ledger.set_meta("paused","true")
        return {"status":"entries_paused","message":"가상 신규 매수 중지. 기존 가상 보유분의 보호 매도·장마감 청산은 계속 시도해요."}

    def resume(self):
        if not self.ledger:return {"status":"unavailable","message":"가상 원장이 준비되지 않았어요."}
        today=self.clock().astimezone(NY).date()
        if today>self.settings.us_paper_end_date:return {"status":"expired","message":"검증 기간이 종료되어 신규 진입을 재개할 수 없어요."}
        if self.ledger:self.ledger.set_meta("paused","false")
        return {"status":"entries_resumed","message":"모의 신규 매수 감시를 재개했어요. 거래일·시간·위험 검사를 통과해야만 가상 체결돼요."}

    async def preflight(self):
        today=self.clock().astimezone(NY).date()
        target=today if self.settings.us_paper_start_date<=today<=self.settings.us_paper_end_date else self.settings.us_paper_start_date
        at=datetime.combine(target,datetime.min.time(),tzinfo=NY)
        try:
            data=await self.market.get('/api/v1/market-calendar/US',{'date':str(target)},ttl=300)
            window=us_session_window(data,at)
            return {'status':'connected','market':'US','currency':'USD','timezone':'America/New_York','date':str(target),'business_day':bool(window),'market_open':window['open'].isoformat() if window else None,'liquidation_start':window['liquidate'].isoformat() if window else None,'market_close':window['close'].isoformat() if window else None,'market_open_kst':window['open'].astimezone(KST).isoformat() if window else None,'liquidation_start_kst':window['liquidate'].astimezone(KST).isoformat() if window else None,'market_close_kst':window['close'].astimezone(KST).isoformat() if window else None,'real_orders_available':False}
        except Exception:
            return {'status':'failed','market':'US','currency':'USD','date':str(target),'message':'미국 공식 정규장 조회 실패. 신규 가상 매수는 보류해요.','real_orders_available':False}
