import asyncio
import time
from collections import OrderedDict, deque

from fastapi import HTTPException


class InMemoryRateLimiter:
    def __init__(
        self, attempts: int, window_seconds: int, max_keys: int = 4096
    ) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.max_keys = max_keys
        self._entries: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def check(self, key: str) -> None:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        async with self._lock:
            entries = self._entries.get(key)
            if entries is None:
                if len(self._entries) >= self.max_keys:
                    # Drop expired identities, never active protection windows.
                    expired = [
                        item
                        for item, values in self._entries.items()
                        if not values or values[-1] <= cutoff
                    ]
                    for item in expired:
                        del self._entries[item]
                if len(self._entries) >= self.max_keys:
                    raise HTTPException(
                        status_code=429, detail="Too many attempts."
                    )
                entries = deque()
                self._entries[key] = entries
            else:
                self._entries.move_to_end(key)
            while entries and entries[0] <= cutoff:
                entries.popleft()
            if len(entries) >= self.attempts:
                raise HTTPException(
                    status_code=429, detail="Too many attempts."
                )
            entries.append(now)


auth_limiter = InMemoryRateLimiter(attempts=8, window_seconds=300)
