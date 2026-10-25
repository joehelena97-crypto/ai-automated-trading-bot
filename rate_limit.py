"""SQLite-backed rate limiter shared by workers using the same local database."""
from __future__ import annotations

import time

from config import settings
from db import consume_rate_limit


class RateLimiter:
    def __init__(self, max_per_minute: int, namespace: str = "default") -> None:
        self.max_per_minute = max_per_minute
        self.namespace = namespace

    def allow(self, key: str = "global") -> bool:
        bucket = f"{self.namespace}:{key}"
        return consume_rate_limit(bucket, self.max_per_minute, 60.0, time.time())


_limiter: RateLimiter | None = None


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(settings.rate_limit_per_minute, "webhook")
    return _limiter
