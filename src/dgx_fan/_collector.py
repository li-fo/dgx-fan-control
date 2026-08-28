from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

Clock = Callable[[], float]
Sample = TypeVar("Sample")


@dataclass(frozen=True)
class CollectorStateSnapshot(Generic[Sample]):
    """Private cache and retry state needed to build an endpoint snapshot."""

    prior: tuple[float, Sample] | None
    age_seconds: float | None
    stale: bool
    error: str | None
    retrying: bool
    retry_attempt: int
    retry_count: int
    failed_attempts: int
    sample_revision: int


class CollectorState(Generic[Sample]):
    """Shared bookkeeping for collectors with one cached sample per endpoint."""

    def __init__(self, stale_after: float, retry_count: int) -> None:
        self.stale_after = stale_after
        self.retry_count = retry_count
        self.last_good: dict[str, tuple[float, Sample]] = {}
        self.errors: dict[str, str | None] = {}
        self.retrying: dict[str, int] = {}
        self.failed_attempts: dict[str, int] = {}
        self.revisions: dict[str, int] = defaultdict(int)

    def record_retry_wait(self, endpoint_id: str, attempt: int) -> None:
        self.retrying[endpoint_id] = attempt + 1

    def record_failure(self, endpoint_id: str, error: str, attempts: int) -> None:
        self.retrying.pop(endpoint_id, None)
        self.errors[endpoint_id] = error
        self.failed_attempts[endpoint_id] = attempts

    def record_success(self, endpoint_id: str, sample: Sample, now: float | Clock | None) -> None:
        self.last_good[endpoint_id] = (self.completed_at(now), sample)
        self.errors[endpoint_id] = None
        self.retrying.pop(endpoint_id, None)
        self.failed_attempts.pop(endpoint_id, None)
        self.revisions[endpoint_id] += 1

    def snapshot(self, endpoint_id: str, now: float | Clock | None) -> CollectorStateSnapshot[Sample]:
        current = self.current_at(now)
        prior = self.last_good.get(endpoint_id)
        age = None if prior is None else max(0.0, current - prior[0])
        stale = age is None or age > self.stale_after
        error = self.errors.get(endpoint_id)
        retry_attempt = self.retrying.get(endpoint_id, 0)
        retrying = retry_attempt > 0
        if prior is None and error is None and not retrying:
            error = "awaiting first sample"
        return CollectorStateSnapshot(
            prior,
            age,
            stale,
            error,
            retrying,
            retry_attempt,
            self.retry_count,
            self.failed_attempts.get(endpoint_id, 0),
            self.revisions[endpoint_id],
        )

    @staticmethod
    def completed_at(now: float | Clock | None) -> float:
        if callable(now):
            return now()
        return time.monotonic() if now is None else now

    @staticmethod
    def current_at(now: float | Clock | None) -> float:
        return time.monotonic() if now is None else now() if callable(now) else now
