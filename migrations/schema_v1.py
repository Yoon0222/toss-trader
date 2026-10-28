from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import String, Numeric, Integer, DateTime, ForeignKey, UniqueConstraint, CheckConstraint, JSON
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

def now():
    return datetime.now(timezone.utc)
class Base(DeclarativeBase):
    pass
class Record:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
class Account(Record, Base):
    __tablename__ = "accounts"
    mode: Mapped[str] = mapped_column(String(20))
    cash: Mapped[Decimal] = mapped_column(Numeric(20, 4))
    __table_args__ = (CheckConstraint("cash >= 0"),)
class Order(Record, Base):
    __tablename__ = "orders"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    client_order_id: Mapped[str] = mapped_column(String(100))
    broker_order_id: Mapped[str | None] = mapped_column(String(100))
    symbol: Mapped[str] = mapped_column(String(20))
    side: Mapped[str] = mapped_column(String(4))
    quantity: Mapped[int] = mapped_column(Integer)
    filled_quantity: Mapped[int] = mapped_column(Integer, default=0)
    limit_price: Mapped[Decimal] = mapped_column(Numeric(20,4))
    status: Mapped[str] = mapped_column(String(24), default="CREATED")
    __table_args__ = (UniqueConstraint("account_id", "client_order_id"), CheckConstraint("quantity > 0"), CheckConstraint("limit_price > 0"), CheckConstraint("filled_quantity >= 0 AND filled_quantity <= quantity"), CheckConstraint("side IN ('BUY','SELL')"))
class Fill(Record, Base):
    __tablename__ = "fills"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.id"))
    execution_id: Mapped[str] = mapped_column(String(100), unique=True)
    quantity: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Numeric(20,4))
    fee: Mapped[Decimal] = mapped_column(Numeric(20,4), default=0)
    __table_args__ = (CheckConstraint("quantity > 0 AND price > 0 AND fee >= 0"),)
class Position(Record, Base):
    __tablename__ = "positions"
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    symbol: Mapped[str] = mapped_column(String(20))
    quantity: Mapped[int] = mapped_column(Integer)
    average_price: Mapped[Decimal] = mapped_column(Numeric(20,4))
    high_watermark: Mapped[Decimal] = mapped_column(Numeric(20,4))
    __table_args__ = (UniqueConstraint("account_id", "symbol"), CheckConstraint("quantity >= 0"))
class MarketSnapshot(Record, Base):
    __tablename__ = "market_snapshots"
    symbol: Mapped[str] = mapped_column(String(20), index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(20,4))
    volume: Mapped[int] = mapped_column(Integer)
class AuditEvent(Record, Base):
    __tablename__ = "audit_events"
    kind: Mapped[str] = mapped_column(String(60), index=True)
    payload: Mapped[dict] = mapped_column(JSON)

metadata = Base.metadata
