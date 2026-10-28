"""Toss official REST read-only adapter. No order submission path."""
import asyncio
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from contextvars import ContextVar

_received_times = ContextVar("received_times", default=None)
import httpx

BASE_URL = "https://openapi.tossinvest.com"
class DataUnavailable(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message

def numeric(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite():
            raise ValueError()
        return str(result)
    except (InvalidOperation, ValueError):
        raise DataUnavailable("invalid_response", "토스 응답의 숫자 형식을 확인할 수 없어요.") from None

def stamp():
    return datetime.now(timezone.utc).isoformat()

class TossMarketData:
    def __init__(self, settings, transport=None):
        self.settings = settings
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=8, transport=transport, follow_redirects=False)
        self._token = None
        self._expires = 0
        self._token_lock = asyncio.Lock()
        self._request_lock = asyncio.Lock()
        self._cache = {}
        self._next_request = 0
        self._auth_blocked_until = 0
        self._auth_error = None
        self._metadata = {}
    @property
    def configured(self):
        return bool(self.settings.toss_client_id.get_secret_value() and self.settings.toss_client_secret.get_secret_value())
    async def close(self):
        self._token = None
        self._cache.clear()
        await self.client.aclose()
    def _check_response(self, response):
        if response.status_code == 401:
            raise DataUnavailable("authentication_failed", "토스 API 인증에 실패했어요. 키와 권한을 확인해 주세요.")
        if response.status_code == 403:
            raise DataUnavailable("access_denied", "토스 API 접근이 거부됐어요. 허용 IP와 계좌 권한을 확인해 주세요.")
        if response.status_code == 429:
            raise DataUnavailable("rate_limited", "토스 API 호출 한도에 도달했어요. 잠시 후 다시 조회해 주세요.")
        if not response.is_success:
            raise DataUnavailable("upstream_error", "토스 API에서 조회를 완료하지 못했어요.")
        try:
            return response.json()
        except ValueError:
            raise DataUnavailable("invalid_response", "토스 응답 형식을 확인할 수 없어요.") from None
    async def token(self):
        if not self.configured:
            raise DataUnavailable("credentials_missing", "토스 API 키 설정이 필요해요.")
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            if time.monotonic() < self._auth_blocked_until:
                raise self._auth_error
            try:
                response = await self.client.post("/oauth2/token", data={"grant_type":"client_credentials", "client_id":self.settings.toss_client_id.get_secret_value(), "client_secret":self.settings.toss_client_secret.get_secret_value()})
                data = self._check_response(response)
                token, ttl = data["access_token"], int(data["expires_in"])
                if not isinstance(token, str) or not token or ttl <= 0:
                    raise ValueError()
                self._token = token
                self._expires = time.monotonic() + max(1, ttl - 60)
                return token
            except (httpx.HTTPError, KeyError, TypeError, ValueError, DataUnavailable) as exc:
                error = exc if isinstance(exc, DataUnavailable) else DataUnavailable("connection_failed", "토스 인증 서버에 연결하지 못했어요.")
                self._auth_error, self._auth_blocked_until = error, time.monotonic()+30
                raise error from None
    async def get(self, path, params=None, account=False, ttl=15):
        key = (path, tuple(sorted((params or {}).items())), account, self.settings.toss_account_seq.get_secret_value() if account else "")
        async with self._request_lock:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                if isinstance(cached[1],DataUnavailable):raise cached[1]
                if _received_times.get() is not None: _received_times.get().append(cached[2])
                return cached[1]
            try:
                token = await self.token()
                headers = {"Authorization": "Bearer "+token}
                if account:
                    seq = self.settings.toss_account_seq.get_secret_value()
                    if not seq:
                        raise DataUnavailable("account_missing", "TOSS_ACCOUNT_SEQ 계좌 식별 설정이 필요해요.")
                    headers["X-Tossinvest-Account"] = seq
                await asyncio.sleep(max(0,self._next_request-time.monotonic()))
                self._next_request = time.monotonic()+.4
                response = await self.client.get(path, params=params, headers=headers)
                if response.status_code == 401:
                    self._token = None
                    self._expires = 0
                data = self._check_response(response)
                result = data["result"]
                received_at=stamp()
                self._cache[key]=(time.monotonic()+ttl,result,received_at)
                if _received_times.get() is not None: _received_times.get().append(received_at)
                return result
            except (httpx.HTTPError,KeyError,TypeError,ValueError,DataUnavailable) as exc:
                error=exc if isinstance(exc,DataUnavailable) else DataUnavailable("connection_failed", "토스 데이터 조회에 실패했어요. 네트워크와 응답 형식을 확인해 주세요.")
                self._cache[key]=(time.monotonic()+30,error)
                raise error from None
    async def section(self, loader):
        received=[]
        context_token=_received_times.set(received)
        try:
            data=await loader()
            return {"status":"connected","data":data,"fetched_at":min(received) if received else None,"error":None}
        except DataUnavailable as exc:
            return {"status":"disconnected","data":None,"fetched_at":None,"error":{"code":exc.code,"message":exc.message}}
        except (TypeError,KeyError,ValueError,AttributeError):
            return {"status":"disconnected","data":None,"fetched_at":None,"error":{"code":"invalid_response","message":"토스 데이터 형식을 확인할 수 없어요."}}
        finally:
            _received_times.reset(context_token)
    async def rankings(self, ranking_type="TOP_GAINERS", duration="1d", count=20, market_country="KR"):
        result=await self.get("/api/v1/rankings",{"type":ranking_type,"marketCountry":market_country,"duration":duration,"count":count})
        rows=result["rankings"]
        symbols=",".join(r["symbol"] for r in rows)
        names_error=None
        if symbols:
            try:
                info=await self.get("/api/v1/stocks",{"symbols":symbols},ttl=3600)
                self._metadata.update({r["symbol"]:r for r in info})
            except DataUnavailable as exc:
                names_error=exc.code
        return {"observed_at":result.get("rankedAt"),"name_lookup_error":names_error,"stocks":[{"code":r["symbol"],"name":self._metadata.get(r["symbol"],{}).get("name",r["symbol"]),"market":self._metadata.get(r["symbol"],{}).get("market", market_country),"currency":r.get("currency"),"security_type":self._metadata.get(r["symbol"],{}).get("securityType"),"price":numeric(r["price"]["lastPrice"]),"change":None if r["price"].get("changeRate") is None else str(Decimal(numeric(r["price"]["changeRate"]))*100),"turnover":numeric(r.get("tradingAmount")),"volume":numeric(r.get("tradingVolume")),"score":None} for r in rows]}
    async def indicators(self):
        rows=await self.get("/api/v1/market-indicators/prices",{"symbols":"KOSPI,KOSDAQ"})
        return [{"symbol":r["symbol"],"value":numeric(r["lastPrice"]),"observed_at":r.get("timestamp")} for r in rows]
    async def exchange(self):
        r=await self.get("/api/v1/exchange-rate",{"baseCurrency":"USD","quoteCurrency":"KRW"},ttl=60)
        return {"value":numeric(r["rate"]),"observed_at":r.get("validFrom")}
    @staticmethod
    def market_currency(market_country):
        if market_country not in {"KR", "US"}:
            raise ValueError("Unsupported market country")
        return "KRW" if market_country == "KR" else "USD"

    async def account(self, market_country="KR"):
        currency = self.market_currency(market_country)
        if not self.settings.toss_account_seq.get_secret_value():
            raise DataUnavailable("account_missing", "TOSS_ACCOUNT_SEQ 계좌 식별 설정이 필요해요.")
        r=await self.get("/api/v1/holdings",account=True)
        positions=[]
        for item in r["items"]:
            if item["marketCountry"] != market_country or item["currency"] != currency:continue
            positions.append({"code":item["symbol"],"name":item["name"],"quantity":numeric(item["quantity"]),"price":numeric(item["lastPrice"]),"average_price":numeric(item["averagePurchasePrice"]),"profit":numeric(item["profitLoss"]["amount"]),"profit_rate":numeric(item["profitLoss"]["rate"])})
        # Official HoldingsOverview has native-currency buckets, never FX-converted totals.
        return {"market_value":numeric(r["marketValue"]["amount"].get(currency.lower())),"unrealized_profit":numeric(r["profitLoss"]["amount"].get(currency.lower())),"daily_valuation_profit":numeric(r["dailyProfitLoss"]["amount"].get(currency.lower())),"positions":positions,"observed_at":None,"currency":currency,"market_country":market_country}
    async def buying_power(self, market_country="KR"):
        currency = self.market_currency(market_country)
        if not self.settings.toss_account_seq.get_secret_value():raise DataUnavailable("account_missing","계좌 식별 설정이 필요해요.")
        r=await self.get("/api/v1/buying-power",{"currency":currency},account=True)
        return {"value":numeric(r["cashBuyingPower"]),"observed_at":None,"currency":currency,"market_country":market_country}
    async def orders(self, market_country="KR"):
        currency = self.market_currency(market_country)
        if not self.settings.toss_account_seq.get_secret_value():raise DataUnavailable("account_missing","계좌 식별 설정이 필요해요.")
        r=await self.get("/api/v1/orders",{"status":"OPEN"},account=True)
        return {"orders":[{"code":o["symbol"],"side":o["side"],"quantity":numeric(o["quantity"]),"price":numeric(o.get("price")),"status":o["status"],"filled_quantity":numeric(o["execution"]["filledQuantity"]),"ordered_at":o["orderedAt"],"currency":o["currency"]} for o in r["orders"] if o["currency"] == currency],"scope":"OPEN","currency":currency,"market_country":market_country,"market_filter":"currency"}
    async def candles(self,symbol,period):
        r=await self.get("/api/v1/candles",{"symbol":symbol,"interval":"1m" if period=="1일" else "1d","count":200 if period=="1일" else (5 if period=="1주" else 22)})
        candles=sorted([{"timestamp":c["timestamp"],"close":numeric(c["closePrice"]),"volume":numeric(c["volume"])} for c in r["candles"]],key=lambda c:c["timestamp"])
        return {"candles":candles,"period":period,"observed_at":candles[-1]["timestamp"] if candles else None,"window":"최근 200분봉" if period=="1일" else "최근 거래일"}
    async def dashboard(self, market_country="KR", include_account=True):
        currency = self.market_currency(market_country)
        async def market_rankings():
            if market_country == "KR":
                return await self.rankings()
            return await self.rankings(ranking_type="MARKET_TRADING_AMOUNT", duration="realtime", count=20, market_country="US")
        async def market_indicators():
            if market_country == "US":
                raise DataUnavailable("unsupported", "미국 시장 지수 조회는 아직 지원하지 않아요.")
            return await self.indicators()
        loaders={"rankings":market_rankings,"indicators":market_indicators,"exchange":self.exchange}
        if include_account:
            loaders.update({"account":lambda:self.account(market_country),"buying_power":lambda:self.buying_power(market_country),"orders":lambda:self.orders(market_country)})
        parts=await asyncio.gather(*(self.section(loader) for loader in loaders.values()))
        return {"provider":"Toss Securities","configured":self.configured,"read_only":True,"market_country":market_country,"currency":currency,"account_included":include_account,"sections":dict(zip(loaders,parts))}
