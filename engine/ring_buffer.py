from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Iterator
from threading import RLock
from typing import Generic, TypeVar

T = TypeVar("T")


class RingBuffer(Generic[T]):
    """Thread-safe fixed-capacity buffer that retains the newest values."""

    def __init__(self, capacity: int) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("Ring buffer capacity must be a positive integer.")
        self.capacity = capacity
        self._values: deque[T] = deque(maxlen=capacity)
        self._lock = RLock()

    def append(self, value: T) -> None:
        with self._lock:
            self._values.append(value)

    def extend(self, values: Iterable[T]) -> None:
        with self._lock:
            self._values.extend(values)

    def snapshot(self) -> tuple[T, ...]:
        with self._lock:
            return tuple(self._values)

    def latest(self) -> T:
        with self._lock:
            if not self._values:
                raise IndexError("Cannot retrieve the latest value of an empty buffer.")
            return self._values[-1]

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def __iter__(self) -> Iterator[T]:
        return iter(self.snapshot())
