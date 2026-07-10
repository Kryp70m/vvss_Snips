import logging
import asyncio

import asyncpg

from app.models.scanner import AlertEvent

logger = logging.getLogger(__name__)


class PostgresStore:
    def __init__(self, database_url: str | None) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        if not self.database_url:
            return
        for attempt in range(1, 11):
            try:
                self.pool = await asyncpg.create_pool(self.database_url.replace("+asyncpg", ""), min_size=1, max_size=5)
                await self.ensure_schema()
                return
            except Exception:
                logger.exception("PostgreSQL unavailable on attempt %s; retrying", attempt)
                await asyncio.sleep(min(attempt, 5))
        logger.error("PostgreSQL unavailable; continuing without alert history")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    async def ensure_schema(self) -> None:
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ignition_alerts (
                    id BIGSERIAL PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    score DOUBLE PRECISION NOT NULL,
                    label TEXT NOT NULL,
                    expected_move TEXT NOT NULL,
                    snapshot JSONB NOT NULL,
                    created_at_ms BIGINT NOT NULL,
                    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now()
                );
                CREATE INDEX IF NOT EXISTS idx_ignition_alerts_symbol_time
                ON ignition_alerts(symbol, created_at_ms DESC);
                """
            )

    async def save_alert(self, alert: AlertEvent) -> None:
        if not self.pool:
            return
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO ignition_alerts
                (symbol, direction, score, label, expected_move, snapshot, created_at_ms)
                VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7)
                """,
                alert.symbol,
                alert.direction,
                alert.score,
                alert.label,
                alert.expected_move,
                alert.snapshot.model_dump_json(),
                alert.created_at,
            )
