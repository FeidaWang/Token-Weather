"""Complete ordered chronology. Replay is explicit and never replays tools."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator, Any

from .models import utc_now
from .storage import WeatherError, append_json


class Ledger:
    def __init__(self, root: Path):
        self.path = root / "ledger.jsonl"

    def rows(self, after: int = 0) -> Iterator[dict[str, Any]]:
        if self.path.is_symlink():
            raise WeatherError("Ledger must not be a symlink")
        if not self.path.exists():
            return
        previous = 0
        with self.path.open("rb") as stream:
            for raw in stream:
                if len(raw) > 65_536 or not raw.endswith(b"\n"):
                    raise WeatherError("Torn/oversized ledger row; keep the last published generation and reconcile the tail")
                try:
                    row = json.loads(raw)
                except (ValueError, UnicodeError) as exc:
                    raise WeatherError("Corrupt ledger row; automatic replay is disabled") from exc
                if row.get("sequence") != previous + 1:
                    raise WeatherError("Out-of-order ledger sequence")
                previous += 1
                if previous > after:
                    yield row

    def append(self, kind: str, summary: str, key: str, payload: dict | None = None) -> dict:
        previous = 0
        for row in self.rows():
            previous = row["sequence"]
            if row["dedup_key"] == key:
                if row["kind"] != kind or row.get("payload", {}) != (payload or {}):
                    raise WeatherError("Deduplication key was reused for a different event")
                return row
        row = {"sequence": previous + 1, "timestamp": utc_now(), "kind": kind,
               "summary": summary, "dedup_key": key, "payload": payload or {}}
        append_json(self.path, row)
        return row

    @staticmethod
    def render_row(row: dict) -> str:
        def clean(value: Any) -> str:
            return str(value).replace("\n", " ").replace("\r", " ").replace("|", "¦")
        update = row.get("payload", {}).get("update", {})
        milestone = update.get("milestone", {})
        evidence = ",".join(milestone.get("evidence", [])) or "none"
        result = milestone.get("result", "recorded; no test result asserted")
        return f'{row["sequence"]:05d} | {row["timestamp"]} | {clean(row["kind"])} | {clean(row["summary"])} | {clean(result)} | evidence: {clean(evidence)}\n'
