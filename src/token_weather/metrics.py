from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import math
import resource
import time
from typing import Iterator

from .models import utc_now
from .storage import append_json, WeatherError, lock

ALLOWED = {"name", "duration_ms", "cpu_ms", "success", "mode", "generation", "bytes_written", "native_compactions_avoided", "input_tokens", "output_tokens", "repeated_calls", "cold_cache"}


def record(root: Path, **values) -> None:
    if set(values) - ALLOWED:
        raise WeatherError("Metrics contain an unsupported field; raw prompts/secrets are not allowed")
    with lock(root / "metrics.lock", timeout=0.1):
        append_json(root / "metrics.jsonl", {"timestamp": utc_now(), **values})


@contextmanager
def measure(root: Path, name: str) -> Iterator[None]:
    start = time.perf_counter()
    cpu = time.process_time()
    success = False
    try:
        yield
        success = True
    finally:
        try:
            record(root, name=name, duration_ms=(time.perf_counter() - start) * 1000,
                   cpu_ms=(time.process_time() - cpu) * 1000, success=success)
        except (WeatherError, OSError):
            import sys
            print("Token Weather: optional metrics write failed; checkpoint result is preserved", file=sys.stderr)


def distribution(samples: list[float]) -> dict:
    if not samples:
        return {"count": 0, "median": None, "p95": None}
    values = sorted(samples)
    n = len(values)
    median = (values[(n - 1) // 2] + values[n // 2]) / 2
    return {"count": n, "median": median, "p95": values[math.ceil(n * .95) - 1]}
