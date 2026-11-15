"""极简滑动窗口限流。

单用户场景不需要分布式，进程内计数就够；目的是「token 泄露时别被人刷爆」。
"""

from __future__ import annotations

import time
from collections import deque
from typing import Deque


class RateLimiter:
    """每个 key（这里用客户端 IP）在 window 秒内最多 limit 次。"""

    def __init__(self, limit: int, window: float = 60.0) -> None:
        self.limit = max(1, int(limit))
        self.window = float(window)
        self._hits: dict[str, Deque[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        dq = self._hits.get(key)
        if dq is None:
            dq = deque()
            self._hits[key] = dq
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.limit:
            return False
        dq.append(now)
        # 顺手清理长期不活跃的 key，避免字典无限增长
        if len(self._hits) > 512:
            for k in [k for k, v in self._hits.items() if not v or now - v[-1] > self.window]:
                self._hits.pop(k, None)
        return True
