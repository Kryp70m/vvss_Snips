import json
import logging

from redis.asyncio import Redis

from app.models.scanner import AlertEvent, MetricSnapshot

logger = logging.getLogger(__name__)


class RedisCache:
    def __init__(self, url: str | None) -> None:
        self.url = url
        self.client: Redis | None = None

    async def connect(self) -> None:
        if self.url:
            try:
                self.client = Redis.from_url(self.url, decode_responses=True)
                await self.client.ping()
            except Exception:
                logger.exception("Redis unavailable; continuing without realtime cache")
                self.client = None

    async def close(self) -> None:
        if self.client:
            await self.client.aclose()

    async def publish_rankings(self, snapshots: list[MetricSnapshot]) -> None:
        if not self.client:
            return
        payload = json.dumps([snapshot.model_dump() for snapshot in snapshots])
        await self.client.set("scanner:rankings", payload, ex=15)
        await self.client.publish("scanner:rankings", payload)

    async def publish_alert(self, alert: AlertEvent) -> None:
        if not self.client:
            return
        payload = alert.model_dump_json()
        await self.client.lpush("scanner:alerts", payload)
        await self.client.ltrim("scanner:alerts", 0, 500)
        await self.client.publish("scanner:alerts", payload)
