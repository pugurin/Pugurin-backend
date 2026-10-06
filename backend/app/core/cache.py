from datetime import datetime, timedelta

from app.core.dates import Clock


class TTLCache[V]:
    """프로세스 내 TTL 캐시. Redis 도입 전 mock 단계에서만 쓴다."""

    def __init__(self, clock: Clock):
        self._clock = clock
        self._items: dict[str, tuple[V, datetime, datetime]] = {}

    def get(self, key: str) -> tuple[V, datetime] | None:
        entry = self._items.get(key)
        if entry is None:
            return None
        value, stored_at, expires_at = entry
        if self._clock() >= expires_at:
            del self._items[key]
            return None
        return value, stored_at

    def set(self, key: str, value: V, ttl: timedelta) -> datetime:
        now = self._clock()
        self._items[key] = (value, now, now + ttl)
        return now
