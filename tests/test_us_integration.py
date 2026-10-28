from datetime import date
import pytest
import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.config import Settings
from app.main import create_app
from app.market import TossMarketData

def test_reject_shared_ledger_directory(tmp_path):
    with pytest.raises(ValidationError):
        Settings(_env_file=None,paper_enabled=True,us_paper_enabled=True,paper_data_dir=tmp_path,us_paper_data_dir=tmp_path)

async def test_us_rankings_use_us_params_and_keep_currency():
    calls=[]
    def respond(request):
        calls.append(request)
        if request.url.path=='/oauth2/token':return httpx.Response(200,json={'access_token':'isolated-token','expires_in':86400})
        if request.url.path=='/api/v1/rankings':
            assert request.url.params['marketCountry']=='US'
            assert request.url.params['duration']=='realtime'
            return httpx.Response(200,json={'result':{'rankedAt':'2026-10-26T10:00:00-04:00','rankings':[{'symbol':'BRK.B','currency':'USD','price':{'lastPrice':'400.12','changeRate':'0.05'},'tradingAmount':'10000001','tradingVolume':'25000'}]}})
        if request.url.path=='/api/v1/stocks':return httpx.Response(200,json={'result':[{'symbol':'BRK.B','name':'Berkshire','market':'NYSE','securityType':'STOCK'}]})
        raise AssertionError(request.url.path)
    market=TossMarketData(Settings(_env_file=None,toss_client_id='test',toss_client_secret='test'),httpx.MockTransport(respond))
    try:
        result=await market.rankings(ranking_type='MARKET_TRADING_AMOUNT',duration='realtime',count=100,market_country='US')
        row=result['stocks'][0]
        assert row['currency']=='USD' and row['market']=='NYSE' and row['security_type']=='STOCK'
        assert row['code']=='BRK.B' and row['change']=='5.00'
        assert all(r.method=='GET' or r.url.path=='/oauth2/token' for r in calls)
    finally:await market.close()

def test_both_engines_have_isolated_state_and_controls(tmp_path):
    cfg=Settings(_env_file=None,paper_enabled=True,us_paper_enabled=True,paper_start_date=date(2099,1,1),paper_end_date=date(2099,1,2),us_paper_start_date=date(2099,1,1),us_paper_end_date=date(2099,1,2),paper_data_dir=tmp_path/'kr',us_paper_data_dir=tmp_path/'us',database_url='sqlite+aiosqlite://',redis_url='redis://127.0.0.1:1/0')
    app=create_app(cfg)
    with TestClient(app) as client:
        kr=client.get('/api/paper/status').json()
        us=client.get('/api/paper/us/status').json()
        assert kr['paper_account']['cash']=='10000000'
        assert us['paper_account']['cash']=='10000' and us['currency']=='USD'
        assert app.state.paper.market is app.state.us_paper.market
        assert app.state.paper.path != app.state.us_paper.path
        assert client.post('/api/paper/us/pause').json()['status']=='entries_paused'
        assert client.get('/api/paper/us/status').json()['entries_paused'] is True
        assert client.get('/api/paper/status').json()['entries_paused'] is False
        assert client.post('/api/paper/us/resume').json()['status']=='entries_resumed'
        status=client.get('/api/status').json()
        assert status['live_orders_available'] is False
        assert status['paper_validation_enabled'] and status['us_paper_validation_enabled']
        assert client.post('/api/paper/us/pause',headers={'origin':'https://foreign.invalid'}).status_code==403
        assert client.get('/api/paper/us/status').headers['cache-control']=='no-store'
        assert client.get('/api/data/us/candles/BRK.B').status_code==200
        assert client.get('/api/data/us/candles/bad$symbol').status_code==422
