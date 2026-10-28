from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from redis.asyncio import Redis
from app.config import Settings

class Resources:
    def __init__(self, settings: Settings):
        self.engine = create_async_engine(settings.database_url.get_secret_value(), pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.redis = Redis.from_url(settings.redis_url.get_secret_value(), socket_connect_timeout=2, socket_timeout=2)
    async def close(self):
        await self.redis.aclose()
        await self.engine.dispose()
