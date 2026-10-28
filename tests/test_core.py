import asyncio
from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.config import Settings, Mode
from app.broker import PaperBroker, MockBroker, TossBroker, OrderRequest, Quote, RiskRejected
from app.models import Base, Account, Order
from app.main import create_app

def request(**kwargs):
    return OrderRequest(**({"client_order_id":"one", "symbol":"005930", "side":"BUY", "quantity":10, "limit_price":"1000"}|kwargs))
def quote(price="1000", age=0):
    return Quote(Decimal(price), datetime.now(timezone.utc)-timedelta(seconds=age))

def test_default_and_live_block():
    assert Settings(_env_file=None).trading_mode == Mode.PAPER
    for enabled in (True, False):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, trading_mode=Mode.LIVE, live_enabled=enabled, live_approved=True)

def test_secrets_hidden():
    assert "my-secret" not in repr(Settings(_env_file=None, toss_client_secret="my-secret"))

@pytest.mark.parametrize("values", [{"quantity":0}, {"quantity":1.5}, {"symbol":"bad"}, {"side":"BAD"}, {"limit_price":"NaN"}, {"limit_price":"-1"}])
def test_order_validation(values):
    with pytest.raises(ValidationError):
        request(**values)

async def test_buy_sell_and_idempotency_concurrency():
    broker=PaperBroker()
    receipts=await asyncio.gather(*[broker.submit(request(), quote()) for _ in range(10)])
    assert all(r == receipts[0] for r in receipts)
    assert broker.cash == Decimal("9990000")
    assert broker.positions["005930"] == 10
    await broker.submit(request(client_order_id="sell", side="SELL", quantity=4), quote("1100"))
    assert broker.cash == Decimal("9994400")
    assert broker.positions["005930"] == 6
    with pytest.raises(RiskRejected):
        await broker.submit(request(quantity=2), quote())

@pytest.mark.parametrize("price,age", [("1000",6),("1000",-10),("0",0),("NaN",0)])
async def test_invalid_quotes(price, age):
    broker=MockBroker()
    with pytest.raises(RiskRejected):
        await broker.submit(request(), quote(price,age))
    assert broker.cash == Decimal("10000000")

async def test_risk_controls():
    broker=PaperBroker(cash=Decimal("100"))
    with pytest.raises(RiskRejected):
        await broker.submit(request(), quote())
    with pytest.raises(RiskRejected):
        await broker.submit(request(side="SELL"), quote())
    broker=PaperBroker()
    with pytest.raises(RiskRejected):
        await broker.submit(request(quantity=1001), quote())
    with pytest.raises(RiskRejected):
        await broker.submit(request(), quote("1001"))
    broker.stopped=True
    with pytest.raises(RiskRejected):
        await broker.submit(request(), quote())

async def test_live_adapter_cannot_submit():
    with pytest.raises(RuntimeError):
        await TossBroker().submit(request(), quote())

async def test_schema_persistence_and_duplicate_constraint(tmp_path):
    engine=create_async_engine("sqlite+aiosqlite:///"+str(tmp_path/"test.db"))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions=async_sessionmaker(engine,expire_on_commit=False)
    async with sessions() as session:
        account=Account(mode="PAPER_TRADING",cash=Decimal("1000000"))
        session.add(account)
        await session.flush()
        account_id=account.id
        session.add(Order(account_id=account_id,client_order_id="same",symbol="005930",side="BUY",quantity=1,limit_price=1000))
        await session.commit()
    await engine.dispose()
    engine=create_async_engine("sqlite+aiosqlite:///"+str(tmp_path/"test.db"))
    async with async_sessionmaker(engine)() as session:
        assert (await session.scalar(select(Account))).id == account_id
        session.add(Order(account_id=account_id,client_order_id="same",symbol="005930",side="BUY",quantity=1,limit_price=1000))
        with pytest.raises(IntegrityError):
            await session.commit()
    await engine.dispose()

def test_api_health_and_dependency_failure():
    settings=Settings(_env_file=None,database_url="sqlite+aiosqlite://", redis_url="redis://127.0.0.1:1/0")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/api/status").json()["live_orders_available"] is False
        response=client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"database":True,"redis":False}
