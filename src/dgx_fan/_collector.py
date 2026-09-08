from __future__ import annotations

import time
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


class CollectorStateMixin(Generic[Sample]):
    """Shared bookkeeping operating directly on a collector's existing state."""

    stale_after: float
    retry_count: int
    _last_good: dict[str, tuple[float, Sample]]
    _errors: dict[str, str | None]
    _retrying: dict[str, int]
    _failed_attempts: dict[str, int]
    _revisions: dict[str, int]

    def _record_retry_wait(self, endpoint_id: str, attempt: int) -> None:
        self._retrying[endpoint_id] = attempt + 1

    def _record_failure(self, endpoint_id: str, error: str, attempts: int) -> None:
        self._retrying.pop(endpoint_id, None)
        self._errors[endpoint_id] = error
        self._failed_attempts[endpoint_id] = attempts

    def _record_success(self, endpoint_id: str, sample: Sample, now: float | Clock | None) -> None:
        self._last_good[endpoint_id] = (self._completed_at(now), sample)
        self._errors[endpoint_id] = None
        self._retrying.pop(endpoint_id, None)
        self._failed_attempts.pop(endpoint_id, None)
        self._revisions[endpoint_id] += 1

    def reset_retry_waits(self) -> None:
        """Clear only an obsolete generation's in-progress retry indicators."""
        self._retrying.clear()

    def _snapshot_state(self, endpoint_id: str, now: float | Clock | None) -> CollectorStateSnapshot[Sample]:
        current = self._current_at(now)
        prior = self._last_good.get(endpoint_id)
        age = None if prior is None else max(0.0, current - prior[0])
        stale = age is None or age > self.stale_after
        error = self._errors.get(endpoint_id)
        retry_attempt = self._retrying.get(endpoint_id, 0)
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
            self._failed_attempts.get(endpoint_id, 0),
            self._revisions[endpoint_id],
        )

    @staticmethod
    def _completed_at(now: float | Clock | None) -> float:
        if callable(now):
            return now()
        return time.monotonic() if now is None else now

    @staticmethod
    def _current_at(now: float | Clock | None) -> float:
        return time.monotonic() if now is None else now() if callable(now) else now
