"""Deterministic, in-memory Phase 1 interfaces. No real network orders."""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Protocol, Literal
from pydantic import BaseModel, Field

class OrderRequest(BaseModel):
    client_order_id: str = Field(min_length=1, max_length=100)
    symbol: str = Field(pattern=r"^\d{6}$")
    side: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0, strict=True)
    limit_price: Decimal = Field(gt=0, allow_inf_nan=False)
@dataclass(frozen=True)
class Quote:
    price: Decimal
    at: datetime
@dataclass(frozen=True)
class Receipt:
    order_id: str
    status: str
    price: Decimal
    quantity: int
class Broker(Protocol):
    async def submit(self, request: OrderRequest, quote: Quote) -> Receipt: ...

class RiskRejected(ValueError):
    pass
class BasicRisk:
    """Phase 1 only: later risk controls must precede real-time automation."""
    def check(self, request, quote, cash, positions, stopped):
        age = (datetime.now(timezone.utc)-quote.at).total_seconds()
        if stopped or not 0 <= age <= 5 or not quote.price.is_finite() or quote.price <= 0:
            raise RiskRejected("stopped or invalid/stale quote")
        if request.quantity * request.limit_price > Decimal("1000000"):
            raise RiskRejected("per-order limit exceeded")
        if request.side == "BUY" and request.quantity * quote.price > cash:
            raise RiskRejected("insufficient cash")
        if request.side == "SELL" and request.quantity > positions.get(request.symbol, 0):
            raise RiskRejected("insufficient position")

class PaperBroker:
    def __init__(self, cash=Decimal("10000000")):
        self.cash = cash
        self.positions = {}
        self.orders = {}
        self.stopped = False
        self.risk = BasicRisk()
        self._lock = asyncio.Lock()
    async def submit(self, request: OrderRequest, quote: Quote) -> Receipt:
        async with self._lock:
            if request.client_order_id in self.orders:
                old, receipt = self.orders[request.client_order_id]
                if old != request:
                    raise RiskRejected("idempotency key conflicts with previous request")
                return receipt
            self.risk.check(request, quote, self.cash, self.positions, self.stopped)
            if (request.side == "BUY" and quote.price > request.limit_price) or (request.side == "SELL" and quote.price < request.limit_price):
                raise RiskRejected("non-marketable limit; pending orders deferred to Phase 6")
            sign = 1 if request.side == "BUY" else -1
            self.cash -= sign * request.quantity * quote.price
            self.positions[request.symbol] = self.positions.get(request.symbol, 0) + sign * request.quantity
            receipt = Receipt("paper-"+request.client_order_id, "FILLED", quote.price, request.quantity)
            self.orders[request.client_order_id] = (request.model_copy(deep=True), receipt)
            return receipt

class MockBroker(PaperBroker):
    """Test broker: caller supplies synthetic quotes explicitly."""

class TossBroker:
    async def submit(self, request: OrderRequest, quote: Quote) -> Receipt:
        raise RuntimeError("Live orders are unavailable in Phase 1")
