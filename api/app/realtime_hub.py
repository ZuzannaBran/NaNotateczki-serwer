import asyncio
import uuid
from collections import defaultdict


class RealtimeHub:
    def __init__(self) -> None:
        self._queues: dict[uuid.UUID, set[asyncio.Queue[dict]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, owner_id: uuid.UUID) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=32)
        async with self._lock:
            self._queues[owner_id].add(queue)
        return queue

    async def unsubscribe(
        self, owner_id: uuid.UUID, queue: asyncio.Queue[dict]
    ) -> None:
        async with self._lock:
            subscribers = self._queues.get(owner_id)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._queues.pop(owner_id, None)

    async def publish(self, owner_id: uuid.UUID, event: dict) -> None:
        async with self._lock:
            queues = list(self._queues.get(owner_id, ()))
        for queue in queues:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # REST reconciliation is authoritative after a dropped signal.
                pass


hub = RealtimeHub()
