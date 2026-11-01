import asyncio
import time
from collections import defaultdict, deque

from fastapi import HTTPException


class InMemoryRateLimiter:
    def __init__(self, attempts: int, window_seconds: int) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._entries: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        async with self._lock:
            entries = self._entries[key]
            while entries and entries[0] <= now - self.window_seconds:
                entries.popleft()
            if len(entries) >= self.attempts:
                raise HTTPException(status_code=429, detail="Too many attempts.")
            entries.append(now)


auth_limiter = InMemoryRateLimiter(attempts=8, window_seconds=300)
