import asyncio
import json
from pathlib import Path
import httpx
import pytest
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.market import TossMarketData, DataUnavailable

def settings(**kwargs):
    return Settings(_env_file=None, toss_client_id="test-client", toss_client_secret="test-secret", **kwargs)

def handler(request):
    if request.url.path == "/oauth2/token":
        assert request.method == "POST"
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200,json={"access_token":"test-token","expires_in":86400})
    assert request.method == "GET"
    assert request.headers["authorization"] == "Bearer test-token"
    path=request.url.path
    if path=="/api/v1/rankings":
        assert request.url.params["type"] == "TOP_GAINERS"
        assert request.url.params["duration"] == "1d"
        result={"rankedAt":"2026-10-26T09:10:00+09:00","rankings":[{"symbol":"0101N0","price":{"lastPrice":"10000","changeRate":"0.0125"},"tradingAmount":"100000000","tradingVolume":"10000"}]}
    elif path=="/api/v1/stocks":result=[{"symbol":"0101N0","name":"테스트 종목","market":"KOSDAQ"}]
    elif path=="/api/v1/candles":result={"candles":[{"timestamp":"2026-10-26T09:02:00+09:00","closePrice":"10100","volume":"20"},{"timestamp":"2026-10-26T09:01:00+09:00","closePrice":"10000","volume":"10"}]}
    elif path=="/api/v1/market-indicators/prices":result=[{"symbol":"KOSPI","lastPrice":"2000","timestamp":"2026-10-26T09:10:00+09:00"}]
    elif path=="/api/v1/exchange-rate":result={"rate":"1300","validFrom":"2026-10-26T09:10:00+09:00"}
    elif path=="/api/v1/holdings":
        assert request.headers["X-Tossinvest-Account"]=="7"
        result={"marketValue":{"amount":{"krw":"10000","usd":"100"}},"profitLoss":{"amount":{"krw":"500","usd":"10"}},"dailyProfitLoss":{"amount":{"krw":"100","usd":"1"}},"items":[{"symbol":"0101N0","name":"테스트","marketCountry":"KR","currency":"KRW","quantity":"1","lastPrice":"10000","averagePurchasePrice":"9500","profitLoss":{"amount":"500","rate":"0.0526"}},{"marketCountry":"US"}]}
    elif path=="/api/v1/buying-power":result={"cashBuyingPower":"20000"}
    elif path=="/api/v1/orders":result={"orders":[]}
    else:raise AssertionError(path)
    return httpx.Response(200,json={"result":result})

async def test_missing_keys_no_network_no_fake_data():
    def forbidden(request):raise AssertionError("network should not be used")
    adapter=TossMarketData(Settings(_env_file=None),httpx.MockTransport(forbidden))
    try:
        data=await adapter.dashboard()
        assert data["configured"] is False
        assert all(section["data"] is None for section in data["sections"].values())
    finally:await adapter.close()

async def test_real_schema_mapping_and_account_semantics():
    requests=[]
    def record(request):requests.append(request);return handler(request)
    adapter=TossMarketData(settings(toss_account_seq="7"),httpx.MockTransport(record))
    try:
        data=await adapter.dashboard()
        assert all(section["status"]=="connected" for section in data["sections"].values())
        stock=data["sections"]["rankings"]["data"]["stocks"][0]
        assert stock["change"]=="1.2500"
        assert stock["code"]=="0101N0"
        assert stock["name"]=="테스트 종목"
        assert stock["score"] is None
        account=data["sections"]["account"]["data"]
        assert account["market_value"]=="10000"
        assert account["daily_valuation_profit"]=="100"
        assert len(account["positions"])==1
        assert data["sections"]["buying_power"]["data"]["value"]=="20000"
        assert "test-token" not in json.dumps(data)
        assert "test-secret" not in json.dumps(data)
        assert all(r.url.path=="/oauth2/token" for r in requests if r.method!="GET")
        assert sum(r.url.path=="/oauth2/token" for r in requests)==1
    finally:await adapter.close()

async def test_candles_sorted():
    adapter=TossMarketData(settings(),httpx.MockTransport(handler))
    try:
        data=await adapter.candles("0101N0","1일")
        assert [c["close"] for c in data["candles"]]==["10000","10100"]
        assert data["window"]=="최근 200분봉"
    finally:await adapter.close()

async def test_single_token_and_cache_concurrent_requests():
    count=0
    def record(request):
        nonlocal count
        if request.url.path=="/oauth2/token":count+=1
        return handler(request)
    adapter=TossMarketData(settings(),httpx.MockTransport(record))
    try:
        await asyncio.gather(*(adapter.indicators() for _ in range(5)))
        assert count==1
    finally:await adapter.close()

@pytest.mark.parametrize("code,expected",[(401,"authentication_failed"),(403,"access_denied"),(429,"rate_limited"),(500,"upstream_error")])
async def test_safe_errors_and_auth_backoff(code,expected):
    calls=0
    def fail(request):
        nonlocal calls
        calls+=1
        return httpx.Response(code,json={"secret":"test-secret","access_token":"test-token"})
    adapter=TossMarketData(settings(),httpx.MockTransport(fail))
    try:
        for _ in range(2):
            result=await adapter.section(adapter.indicators)
            assert result["data"] is None
            assert result["error"]["code"]==expected
            assert "test-secret" not in json.dumps(result)
        assert calls==1
    finally:await adapter.close()

async def test_timeout_has_no_fake_fallback():
    def fail(request):raise httpx.ReadTimeout("private upstream detail")
    adapter=TossMarketData(settings(),httpx.MockTransport(fail))
    try:
        result=await adapter.section(adapter.indicators)
        assert result["data"] is None
        assert "private" not in json.dumps(result)
    finally:await adapter.close()

async def test_empty_rankings_nullable_time():
    def empty(request):
        if request.url.path=="/oauth2/token":return handler(request)
        return httpx.Response(200,json={"result":{"rankings":[],"rankedAt":None}})
    adapter=TossMarketData(settings(),httpx.MockTransport(empty))
    try:
        result=await adapter.rankings()
        assert result["stocks"]==[]
        assert result["observed_at"] is None
    finally:await adapter.close()

def test_local_data_endpoint_guard_and_cache_headers():
    cfg=Settings(_env_file=None,database_url="sqlite+aiosqlite://",redis_url="redis://127.0.0.1:1/0")
    with TestClient(create_app(cfg)) as client:
        response=client.get("/api/data/dashboard")
        assert response.status_code==200
        assert response.headers["cache-control"]=="no-store"
        assert response.json()["configured"] is False
        assert client.get("/api/data/dashboard",headers={"sec-fetch-site":"cross-site"}).status_code==403
        assert client.get("/api/data/dashboard",headers={"host":"attacker.example"}).status_code==400
        assert client.get("/api/data/candles/bad").status_code==422
        assert client.get("/api/data/candles/0101N0?period=wrong").status_code==422

def test_dashboard_has_no_generated_financial_data():
    root=Path(__file__).parents[1]/"app/static"
    js=(root/"dashboard.js").read_text(encoding="utf-8")
    html=(root/"index.html").read_text(encoding="utf-8")
    assert "Math.sin" not in js
    assert "const samples" not in js
    assert "142,800" not in html
    assert "샘플" not in html

async def test_cached_data_preserves_received_time():
    adapter=TossMarketData(settings(),httpx.MockTransport(handler))
    try:
        first=await adapter.section(adapter.indicators)
        second=await adapter.section(adapter.indicators)
        assert first["fetched_at"] is not None
        assert first["fetched_at"] == second["fetched_at"]
    finally:await adapter.close()

async def test_revoked_token_is_replaced_on_next_refresh():
    token_count=0
    def revoke(request):
        nonlocal token_count
        if request.url.path=="/oauth2/token":
            token_count+=1
            return httpx.Response(200,json={"access_token":"token-"+str(token_count),"expires_in":86400})
        if request.headers["authorization"]=="Bearer token-1":return httpx.Response(401,json={})
        return httpx.Response(200,json={"result":[]})
    adapter=TossMarketData(settings(),httpx.MockTransport(revoke))
    try:
        first=await adapter.section(adapter.indicators)
        assert first["error"]["code"]=="authentication_failed"
        assert adapter._token is None
        adapter._cache.clear()  # simulate error-cache expiry before next refresh
        second=await adapter.section(adapter.indicators)
        assert second["status"]=="connected"
        assert token_count==2
    finally:await adapter.close()


async def test_market_account_currency_isolation_and_native_totals():
    adapter = TossMarketData(settings(toss_account_seq="7"))
    calls = []
    def holding(country, currency, symbol):
        return {"marketCountry":country,"currency":currency,"symbol":symbol,"name":symbol,"quantity":"1","lastPrice":"100","averagePurchasePrice":"90","profitLoss":{"amount":"10","rate":"0.1"}}
    def order(currency, symbol):
        return {"currency":currency,"symbol":symbol,"side":"BUY","quantity":"1","price":None,"status":"PENDING","execution":{"filledQuantity":"0"},"orderedAt":"2026-10-26T10:00:00Z"}
    async def fake_get(path, params=None, account=False, **kwargs):
        calls.append((path, params, account))
        assert account
        if path.endswith("holdings"):
            return {"marketValue":{"amount":{"krw":"100000","usd":"100"}},"profitLoss":{"amount":{"krw":"10000","usd":"10"}},"dailyProfitLoss":{"amount":{"krw":"1000","usd":"1"}},"items":[holding("KR","KRW","005930"),holding("US","USD","AAPL"),holding("KR","USD","BAD1"),holding("US","KRW","BAD2")]}
        if path.endswith("buying-power"):
            return {"cashBuyingPower":"250" if params["currency"] == "USD" else "250000"}
        return {"orders":[order("KRW","005930"),order("USD","AAPL"),order("EUR","OTHER")]}
    adapter.get = fake_get
    try:
        kr, us = await adapter.account(), await adapter.account("US")
        assert kr["market_value"] == "100000" and us["market_value"] == "100"
        assert us["unrealized_profit"] == "10" and us["daily_valuation_profit"] == "1"
        assert [p["code"] for p in kr["positions"]] == ["005930"]
        assert [p["code"] for p in us["positions"]] == ["AAPL"]
        assert (await adapter.buying_power("US"))["value"] == "250"
        assert (await adapter.buying_power())["value"] == "250000"
        assert [o["code"] for o in (await adapter.orders("US"))["orders"]] == ["AAPL"]
        assert [o["code"] for o in (await adapter.orders())["orders"]] == ["005930"]
        with pytest.raises(ValueError): await adapter.account("JP")
    finally:
        await adapter.close()


async def test_us_dashboard_market_only_never_calls_account_loaders():
    adapter = TossMarketData(settings())
    calls = []
    async def rankings(**kwargs):
        calls.append(kwargs)
        return {"stocks":[],"observed_at":None}
    async def exchange(): return {"value":"1300"}
    async def indicators(): return []
    async def forbidden(*args, **kwargs): raise AssertionError("account request must be skipped")
    adapter.rankings = rankings
    adapter.exchange = exchange
    adapter.indicators = indicators
    adapter.account = adapter.buying_power = adapter.orders = forbidden
    try:
        result = await adapter.dashboard("US", include_account=False)
        assert result["currency"] == "USD" and result["account_included"] is False
        assert set(result["sections"]) == {"rankings","indicators","exchange"}
        assert result["sections"]["indicators"]["error"]["code"] == "unsupported"
        assert calls == [{"ranking_type":"MARKET_TRADING_AMOUNT","duration":"realtime","count":20,"market_country":"US"}]
        calls.clear()
        result = await adapter.dashboard(include_account=False)
        assert result["market_country"] == "KR"
        assert calls == [{}]
    finally:
        await adapter.close()


def test_both_dashboard_routes_forward_account_query():
    cfg=Settings(_env_file=None,database_url="sqlite+aiosqlite://",redis_url="redis://127.0.0.1:1/0")
    app=create_app(cfg)
    with TestClient(app) as client:
        calls=[]
        async def dashboard(market_country="KR", include_account=True):
            calls.append((market_country, include_account))
            return {"market_country":market_country,"account_included":include_account}
        app.state.market.dashboard=dashboard
        for path in ("/api/data/dashboard", "/api/data/us/dashboard"):
            response=client.get(path+"?account=false")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            assert response.json()["account_included"] is False
            assert client.get(path).status_code == 200
        assert calls == [("KR",False),("KR",True),("US",False),("US",True)]
