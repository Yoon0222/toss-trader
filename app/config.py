from enum import StrEnum
from datetime import date
from pathlib import Path
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER_TRADING"
    LIVE = "LIVE_TRADING"

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql+asyncpg://trader:trader@localhost:5432/trader")
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    trading_mode: Mode = Mode.PAPER
    live_enabled: bool = False
    live_approved: bool = False
    toss_client_id: SecretStr = SecretStr("")
    toss_client_secret: SecretStr = SecretStr("")
    toss_account_seq: SecretStr = SecretStr("")
    paper_enabled: bool = False
    paper_start_date: date = date(2026,10,27)
    paper_end_date: date = date(2026,11,2)
    paper_data_dir: Path = Path("data/paper-validation-20261027")
    us_paper_enabled: bool = False
    us_paper_start_date: date = date(2026,10,26)
    us_paper_end_date: date = date(2026,10,30)
    us_paper_data_dir: Path = Path("data/us-paper-validation-20261026")
    @model_validator(mode="after")
    def block_live(self):
        if self.paper_enabled and (self.trading_mode != Mode.PAPER or self.paper_end_date < self.paper_start_date):
            raise ValueError("Paper validation requires PAPER mode and a valid date range")
        if self.us_paper_enabled and self.paper_enabled and self.us_paper_data_dir.resolve() == self.paper_data_dir.resolve():
            raise ValueError("KR and US paper ledgers must use separate directories")
        if self.us_paper_enabled and (self.trading_mode != Mode.PAPER or self.us_paper_end_date < self.us_paper_start_date):
            raise ValueError("US paper validation requires PAPER mode and a valid date range")
        if self.trading_mode == Mode.LIVE:
            raise ValueError("Phase 1 does not implement live trading, regardless of approval flags")
        return self
