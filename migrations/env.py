import asyncio
from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine
from app.config import Settings
from app.models import Base

def run(connection):
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
async def online():
    engine=create_async_engine(Settings().database_url.get_secret_value())
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()
asyncio.run(online())
