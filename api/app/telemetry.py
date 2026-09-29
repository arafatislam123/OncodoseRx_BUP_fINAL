"""Request metrics for Prometheus, plus a rolling window for the health page's p95 and error rate."""

from __future__ import annotations

import time
from collections import deque

from fastapi import Request
from prometheus_client import Counter, Histogram
from starlette.middleware.base import BaseHTTPMiddleware

REQUESTS = Counter("http_requests_total", "HTTP requests", ["service", "route", "code"])
DURATION = Histogram("http_request_duration_seconds", "HTTP request duration", ["service", "route"],
                     buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5))


class Window:
    def __init__(self, seconds: float = 60.0) -> None:
        self.seconds = seconds
        self.items: deque[tuple[float, float, bool]] = deque()

    def add(self, duration: float, error: bool) -> None:
        now = time.monotonic()
        self.items.append((now, duration, error))
        while self.items and now - self.items[0][0] > self.seconds:
            self.items.popleft()

    def p95_ms(self) -> float:
        if not self.items:
            return 0.0
        values = sorted(d for _, d, _ in self.items)
        return round(values[min(len(values) - 1, int(0.95 * len(values)))] * 1000, 1)

    def error_rate(self) -> float:
        if not self.items:
            return 0.0
        return round(sum(1 for _, _, e in self.items if e) / len(self.items), 4)


window = Window()


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        code = 500
        try:
            response = await call_next(request)
            code = response.status_code
            return response
        finally:
            elapsed = time.perf_counter() - started
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            if path not in ("/metrics", "/api/ws"):
                REQUESTS.labels("api", path, str(code)).inc()
                DURATION.labels("api", path).observe(elapsed)
                window.add(elapsed, code >= 500)
