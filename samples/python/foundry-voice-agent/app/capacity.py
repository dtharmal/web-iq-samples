# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

"""Admission is synchronous/atomic on the one ASGI event loop."""

import time
from collections import deque


class Admission:
    def __init__(self, *, clock=time.monotonic):
        self.clock = clock
        self.active: set[str] = set()
        self.starts: deque[float] = deque()

    def acquire(self, subject: str, limit: int) -> bool:
        now = self.clock()
        while self.starts and now - self.starts[0] >= 10:
            self.starts.popleft()
        if subject in self.active or len(self.active) >= limit or len(self.starts) >= 20:
            return False
        self.active.add(subject)
        self.starts.append(now)
        return True

    def release(self, subject: str) -> None:
        self.active.discard(subject)
