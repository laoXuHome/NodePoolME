"""Mutual exclusion between node detection/scoring and download-speed batches.

Both jobs drive the same test engine and the same local proxy ports, so they must
never run at the same time.  A small owner gate makes the exclusion explicit
(and observable for the UI) instead of relying only on an anonymous lock.
"""
import asyncio
from datetime import datetime


class RuntimeGate:
    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self.owner: str | None = None
        self.label = ""
        self.since: str | None = None

    def snapshot(self) -> dict:
        return {
            "owner": self.owner,
            "label": self.label,
            "busy": self.owner is not None,
            "since": self.since,
        }

    async def try_acquire(self, owner: str, label: str) -> bool:
        async with self._guard:
            if self.owner is not None:
                return False
            self.owner = owner
            self.label = label
            self.since = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return True

    async def acquire_wait(self, owner: str, label: str, poll: float = 0.5) -> None:
        while not await self.try_acquire(owner, label):
            await asyncio.sleep(max(0.2, poll))

    async def release(self, owner: str) -> None:
        async with self._guard:
            if self.owner == owner:
                self.owner = None
                self.label = ""
                self.since = None


gate = RuntimeGate()
