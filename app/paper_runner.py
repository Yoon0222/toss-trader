"""Bounded, durable forward-test engine. Uses read-only market data only."""
import asyncio
import contextlib
import json
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from app.paper_ledger import PaperLedger

KST=timezone(timedelta(hours=9), "Asia/Seoul")
D=Decimal

def parse_time(value):
    result=datetime.fromisoformat(str(value).replace("Z","+00:00"))
    if result.tzinfo is None:raise ValueError("timezone required")
    return result

def finite(value):
    n=D(str(value))
    if not n.is_finite():raise ValueError("nonfinite")
    return n

def fresh(observed,now,max_age=15):
    age=(now-parse_time(observed)).total_seconds()
    return -2<=age<=max_age

def session_window(calendar,now):
    today=calendar["today"]
    if today["date"]!=now.astimezone(KST).date().isoformat():raise ValueError("calendar date mismatch")
    regular=(today.get("integrated") or {}).get("regularMarket")
    if not regular:return None
    opened,closed=parse_time(regular["startTime"]),parse_time(regular["endTime"])
    if closed<=opened:raise ValueError("invalid session")
    continuous_end=parse_time(regular.get("singlePriceAuctionStartTime") or regular["endTime"])
    return {"open":opened,"buy_start":opened+timedelta(minutes=5),"liquidate":min(closed-timedelta(minutes=30),continuous_end-timedelta(minutes=5)),"continuous_end":continuous_end,"close":closed}

def breakout_signal(candles,now):
    rows=sorted([c for c in candles if parse_time(c["timestamp"])+timedelta(minutes=1)<=now and parse_time(c["timestamp"]).astimezone(KST).date()==now.astimezone(KST).date()],key=lambda c:c["timestamp"])
    if len(rows)<22:return None
    current=rows[-1];previous=rows[-21:-1]
    if not fresh(current["timestamp"],now,180):return None
    volume=finite(current["volume"])
    avg=sum((finite(c["volume"]) for c in previous),D(0))/20
    prior_high=max(finite(c["highPrice"]) for c in previous)
    close=finite(current["closePrice"])
    total_volume=sum((finite(c["volume"]) for c in rows),D(0))
    if total_volume<=0 or avg<=0:return None
    vwap=sum(((finite(c["highPrice"])+finite(c["lowPrice"])+finite(c["closePrice"]))/3*finite(c["volume"]) for c in rows),D(0))/total_volume
    if volume>=max(avg*2,D(10000)) and close>prior_high and close>vwap and close>finite(current["openPrice"]):
        return {"candle_at":current["timestamp"],"close":str(close),"volume_ratio":str(volume/avg),"vwap":str(vwap)}
    return None

def executable_quote(book,side,wanted,now,slippage=D("0.0005"),max_spread=D("0.005"),currency="KRW"):
    if book.get("currency")!=currency or not fresh(book.get("timestamp"),now):raise ValueError("stale_or_invalid_quote")
    asks=sorted([(finite(r["price"]),int(finite(r["volume"]))) for r in book["asks"]],key=lambda r:r[0])
    bids=sorted([(finite(r["price"]),int(finite(r["volume"]))) for r in book["bids"]],reverse=True)
    if not asks or not bids or asks[0][0]<=0 or bids[0][0]<=0 or asks[0][0]<bids[0][0]:raise ValueError("invalid_orderbook")
    if side=="BUY" and (asks[0][0]-bids[0][0])/asks[0][0]>max_spread:raise ValueError("wide_spread")
    # A marketable limit at the top level. Conservative participation: at most 10% of displayed size.
    price,size=asks[0] if side=="BUY" else bids[0]
    quantity=min(wanted,max(0,size//10))
    if quantity<=0:raise ValueError("insufficient_displayed_liquidity")
    price=(price*(1+slippage if side=="BUY" else 1-slippage)).quantize(D("0.0001"))
    return quantity,price

class WorkerLock:
    def __init__(self,path):self.path=Path(path);self.handle=None
    def acquire(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.handle=self.path.open("a+b");self.handle.seek(0)
        if self.path.stat().st_size==0:self.handle.write(b"0");self.handle.flush()
        self.handle.seek(0)
        try:
            import os
            if os.name=="nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            return True
        except OSError:self.handle.close();self.handle=None;return False
    def close(self):
        if self.handle:self.handle.close();self.handle=None

class PaperValidation:
    def __init__(self,market,settings,clock=None):
        self.market=market;self.settings=settings
        self.clock=clock or (lambda: datetime.now(KST))
        self.path=Path(settings.paper_data_dir)
        self.lock=WorkerLock(self.path/"worker.lock")
        self.ledger=None;self.task=None;self.phase="disabled";self.last_attempt=None;self.last_success=None;self.error=None
    async def start(self):
        if not self.settings.paper_enabled:return
        if not self.lock.acquire():self.phase="locked";self.error="다른 모의검증 프로세스가 실행 중이에요.";return
        try:
            self.ledger=PaperLedger(self.path/"ledger.sqlite3",D("10000000"))
            # Never reset an existing experiment automatically.
            configured={"start":str(self.settings.paper_start_date),"end":str(self.settings.paper_end_date),"initial_cash":"10000000","per_symbol":"equity_fraction","position_fraction":"0.5","max_positions":3,"daily_loss":"200000","fee_rate":"0.00015","sell_tax_assumption":"0.002","slippage":"0.0005"}
            stored=self.ledger.get_meta("experiment")
            legacy_configured = dict(configured, per_symbol="1000000")
            legacy_configured.pop("position_fraction")
            previous_configured = json.loads(stored) if stored else None
            capital_upgraded = previous_configured == legacy_configured
            if previous_configured and previous_configured not in (configured, legacy_configured):
                raise ValueError("experiment_config_changed")
            strategy={"version":"volume-breakout-v3","universe":"MARKET_TRADING_AMOUNT/realtime/top100","change_min":"3","change_max":"10","turnover_min":"1000000000","volume_multiple":"2","minute_volume_min":"10000","breakout_lookback":20,"candle_limit":60,"quote_max_age_seconds":15,"signal_max_age_seconds":180,"stop":"-1%","partial":"+3%/50%","trailing":"-2%","max_hold_minutes":None,"daily_entries":5,"position_fraction":"0.5","max_consecutive_losses":3}
            saved=self.ledger.get_meta("strategy")
            previous_strategy = json.loads(saved) if saved else None
            timed_strategy = dict(strategy, version="volume-breakout-v2", max_hold_minutes=90)
            legacy_strategy = dict(timed_strategy, version="volume-breakout-v1")
            legacy_strategy.pop("position_fraction")
            if previous_strategy and previous_strategy not in (strategy, timed_strategy, legacy_strategy):raise ValueError("strategy_changed")
            self.ledger.set_meta("experiment",json.dumps(configured))
            # Commit the policy version and its single audit event together.
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("strategy",json.dumps(json.dumps(strategy))))
                if previous_strategy and previous_strategy != strategy:
                    db.execute("INSERT INTO events(kind,message,at) VALUES(?,?,?)", ("max_hold_removed","90분 보유 강제매도 제거. 손절·트레일링·분할익절·장마감 청산 유지.",self.clock().isoformat()))
            if capital_upgraded:
                self.event("position_sizing_changed", "종목당 진입 한도를 현재 가상 순자산의 50%로 변경. 수수료 포함·현금 한도 적용, 기존 체결과 잔고 보존.", self.clock())
            self.phase="scheduled";self.task=asyncio.create_task(self.run(),name="paper-validation")
        except Exception:
            self.phase="error";self.error="가상 원장 복구 또는 검증 설정을 확인해 주세요.";self.lock.close()
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
        return {"enabled":self.settings.paper_enabled,"phase":self.phase,"error":self.error,"last_attempt":self.last_attempt,"last_success":self.last_success,"start_date":str(self.settings.paper_start_date),"end_date":str(self.settings.paper_end_date),"real_orders_available":False,"paper_account":state,"last_scan":json.loads(self.ledger.get_meta("last_scan","null")) if self.ledger else None,"entries_paused":bool(self.ledger and self.ledger.get_meta("paused","false")=="true"),"equity":str(self.equity(state)) if state else None,"rules":{"initial_cash":"10000000","per_symbol":str(self.entry_budget(state)) if state else None,"position_fraction":"0.5","sizing_basis":"cash_plus_last_marked_positions","max_positions":3,"daily_loss_limit":"200000","max_hold_minutes":None,"stop_loss":"-1%","partial_take":"+3% 시 50%","trailing":"-2%","fees":"편도 0.015%","sell_tax_assumption":"0.2%","slippage":"편도 0.05%"}}
    def report(self,day,now,final=False):
        state=self.ledger.snapshot();equity=self.equity(state)
        peak=finite(self.ledger.get_meta("peak_equity","10000000"));peak=max(peak,equity)
        drawdown=(equity/peak-1)*100 if peak else D(0)
        worst=min(finite(self.ledger.get_meta("mdd","0")),drawdown)
        self.ledger.set_meta("peak_equity",str(peak));self.ledger.set_meta("mdd",str(worst))
        report={"day":day,"generated_at":now.isoformat(),"final":final,"cash":state["cash"],"equity_marked":str(equity),"net_profit_marked":str(equity-D("10000000")),"return_pct":str((equity/D("10000000")-1)*100),"realized_profit":state["realized_profit"],"open_positions":len(state["positions"]),"unliquidated":bool(state["positions"]),"max_drawdown_observed_pct":str(worst),"valuation_note":"마지막 정상 호가 기준 가상평가. 미청산 수익은 확정 아님.","totals":state["totals"],"daily":self.ledger.daily_stats(day),"cost_model":"수수료 편도0.015%, 매도세금가정0.2%, 슬리피지 편도0.05%; 실제 부과율 아님"}
        self.ledger.save_report(day,report)
        report_dir=self.path/"reports";report_dir.mkdir(exist_ok=True)
        target=report_dir/(day+("-final" if final else "")+".json");temp=target.with_suffix(".tmp")
        temp.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8");temp.replace(target)
        return report
    async def run(self):
        while True:
            try:await self.tick(self.clock())
            except asyncio.CancelledError:raise
            except Exception:
                self.phase="error";self.error="모의검증 처리 실패. 신규 진입을 보류했어요. 연결·가상 원장을 확인해 주세요."
                try:self.event("error",self.error,self.clock())
                except Exception:pass
            await asyncio.sleep(30)
    async def tick(self,now):
        now=now.astimezone(KST);self.last_attempt=now.isoformat()
        day=now.date();start=self.settings.paper_start_date;end=self.settings.paper_end_date
        if day<start:self.phase="scheduled";return
        if day>end:
            self.phase="completed_with_positions" if self.ledger.snapshot()["positions"] else "completed"
            if not self.ledger.get_meta("final_report"):
                self.report(str(end),now,True);self.ledger.set_meta("final_report","done")
            return
        if now.hour<8 or (now.hour==8 and now.minute<30):self.phase="waiting_market";return
        if self.ledger.get_meta("closed:"+str(day)):
            self.phase="unliquidated" if self.ledger.snapshot()["positions"] else ("completed" if day==end else "daily_complete")
            return
        calendar=await self.market.get("/api/v1/market-calendar/KR",{"date":str(day)},ttl=300)
        window=session_window(calendar,now)
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
        peak=max(finite(self.ledger.get_meta("peak_equity","10000000")),equity)
        self.ledger.set_meta("peak_equity",str(peak))
        self.ledger.set_meta("mdd",str(min(finite(self.ledger.get_meta("mdd","0")),(equity/peak-1)*100)))
        baseline=finite(self.ledger.get_meta("day_start:"+str(day)))
        loss=equity-baseline
        if loss<=-D("200000"):self.ledger.set_meta("loss_stop:"+str(day),"true")
        halted=self.ledger.get_meta("loss_stop:"+str(day))=="true" or self.ledger.daily_stats(str(day))["consecutive_losses"]>=3
        if self.error:
            self.phase="degraded";return
        if paused or halted or now<window["buy_start"] or now>=window["liquidate"] or now>=window["continuous_end"]:
            self.phase="paused_entries" if paused else "loss_limit" if halted else "liquidating" if now>=window["liquidate"] else "waiting_entry"
            self.last_success=now.isoformat();return
        if self.error:
            self.phase="degraded";return
        if len(state["positions"])>=3:self.last_success=now.isoformat();return
        if self.ledger.daily_stats(str(day))["entry_count"]>=5:self.phase="daily_entry_limit";return
        ranking=await self.market.rankings(ranking_type="MARKET_TRADING_AMOUNT",duration="realtime",count=100)
        if not ranking["observed_at"] or not fresh(ranking["observed_at"],self.clock(),120):raise ValueError("ranking_stale")
        for stock in ranking["stocks"]:
            state=self.ledger.snapshot()
            if len(state["positions"])>=3:break
            if self.ledger.daily_stats(str(day))["entry_count"]>=5:break
            if stock["market"] not in {"KOSPI","KOSDAQ"}:continue
            if any(p["symbol"]==stock["code"] for p in state["positions"]):continue
            if stock["change"] is None or not D("3")<=finite(stock["change"])<=D("10"):continue
            if finite(stock.get("turnover") or 0)<D("1000000000"):continue
            candles=await self.market.get("/api/v1/candles",{"symbol":stock["code"],"interval":"1m","count":60},ttl=15)
            signal=breakout_signal(candles["candles"],self.clock())
            if not signal:continue
            # Ensure session did not expire while REST requests were running.
            at=self.clock()
            if at>=window["liquidate"]:break
            book=await self.market.get("/api/v1/orderbook",{"symbol":stock["code"]},ttl=0)
            at=self.clock()
            if not (self.settings.paper_start_date<=at.date()<=self.settings.paper_end_date and window["buy_start"]<=at<window["liquidate"] and at<window["continuous_end"]):break
            ask=min(finite(r["price"]) for r in book["asks"])
            if ask<=0 or not D("0.99")<=ask/finite(signal["close"])<=D("1.01"):continue
            budget=self.entry_budget(state)
            wanted=int((budget/(ask*D("1.0005")*D("1.00015"))).to_integral_value(rounding=ROUND_DOWN))
            if wanted<=0:continue
            try:qty,price=executable_quote(book,"BUY",wanted,self.clock())
            except ValueError:
                self.event("entry_skipped","호가 신선도·유동성 조건 미충족: "+stock["code"],at);continue
            qty=min(qty,int((budget/(price*D("1.00015"))).to_integral_value(rounding=ROUND_DOWN)))
            if qty<=0:continue
            at=self.clock()
            if not (window["buy_start"]<=at<window["liquidate"] and at<window["continuous_end"] and self.settings.paper_start_date<=at.date()<=self.settings.paper_end_date):break
            key=f"entry:{day}:{stock['code']}:{signal['candle_at']}"
            if self.ledger.execute(key=key,symbol=stock["code"],name=stock["name"],side="BUY",quantity=qty,price=price,observed_at=book["timestamp"],received_at=self.clock().isoformat(),reason="volume_breakout",fee_rate=D("0.00015"),tax_rate=D(0),at=at.isoformat()):
                self.event("paper_buy","실제 호가 기반 가상 매수: "+stock["code"],at)
        self.ledger.set_meta("last_scan",json.dumps({"at":self.clock().isoformat(),"universe_count":len(ranking["stocks"]),"strategy":"volume-breakout-v3"}))
        self.last_success=self.clock().isoformat()
    async def manage_position(self,position,now,window):
        try:
            book=await self.market.get("/api/v1/orderbook",{"symbol":position["symbol"]},ttl=0)
            at=self.clock()
            if at>=window["continuous_end"]:raise ValueError("auction_session")
            qty,bid=executable_quote(book,"SELL",position["quantity"],at)
            self.ledger.mark(position["symbol"],bid)
            average=finite(position["average_price"]);high=max(finite(position["high_price"]),bid)
            overnight=parse_time(position["opened_at"]).astimezone(KST).date()<at.date()
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
            self.ledger.execute(key=key,symbol=position["symbol"],name=position["name"],side="SELL",quantity=qty,price=bid,observed_at=book["timestamp"],received_at=at.isoformat(),reason=reason,fee_rate=D("0.00015"),tax_rate=D("0.002"),at=at.isoformat(),partial=partial)
        except Exception:
            self.error="가상 보유 종목의 호가·청산 확인 실패. 신규 매수 보류, 다음 주기에 재확인해요."
            self.event("exit_deferred",self.error+" "+position["symbol"],now)
    def pause(self):
        if not self.ledger:return {"status":"unavailable","message":"가상 원장이 준비되지 않았어요."}
        if self.ledger:self.ledger.set_meta("paused","true")
        return {"status":"entries_paused","message":"가상 신규 매수 중지. 기존 가상 보유분의 보호 매도·장마감 청산은 계속 시도해요."}

    def resume(self):
        if not self.ledger:return {"status":"unavailable","message":"가상 원장이 준비되지 않았어요."}
        today=self.clock().date()
        if today>self.settings.paper_end_date:return {"status":"expired","message":"검증 기간이 종료되어 신규 진입을 재개할 수 없어요."}
        if self.ledger:self.ledger.set_meta("paused","false")
        return {"status":"entries_resumed","message":"모의 신규 매수 감시를 재개했어요. 거래일·시간·위험 검사를 통과해야만 가상 체결돼요."}

    async def preflight(self):
        at=datetime.combine(self.settings.paper_start_date,datetime.min.time(),tzinfo=KST)
        try:
            data=await self.market.get("/api/v1/market-calendar/KR",{"date":str(at.date())},ttl=300)
            window=session_window(data,at)
            return {"status":"connected","date":str(at.date()),"business_day":bool(window),"market_open":window["open"].isoformat() if window else None,"liquidation_start":window["liquidate"].isoformat() if window else None,"market_close":window["close"].isoformat() if window else None,"real_orders_available":False}
        except Exception:
            return {"status":"failed","date":str(at.date()),"message":"공식 거래일·세션 조회를 확인하지 못했어요. 실패 시 신규 가상 매수는 보류해요.","real_orders_available":False}
