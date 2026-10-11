from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from .models import utc_now
from .storage import canonical_json, digest, read_json, atomic_json, read_bytes, WeatherError


def file_fingerprint(path: Path, line_range: tuple[int, int] | None = None) -> dict:
    canonical = path.resolve(strict=True)
    raw = read_bytes(path, 4 * 1024 * 1024)
    if line_range:
        start, end = line_range
        if start < 1 or end < start:
            raise WeatherError("Invalid evidence line range")
        raw = b"".join(raw.splitlines(keepends=True)[start - 1:end])
    return {"path": str(canonical), "range": list(line_range) if line_range else None, "sha256": digest(raw)}


class VerificationCache:
    def __init__(self, root: Path):
        self.path = root / "verification-cache.json"

    def record(self, files: list[Path], command: list[str], config: dict, conclusion: str,
               dependencies: list[Path] | None = None, expiry_seconds: float | None = None,
               environment: dict[str, str] | None = None, external: bool = False,
               line_ranges: dict[str, tuple[int, int]] | None = None) -> str:
        if external and expiry_seconds is None:
            raise WeatherError("External evidence needs an explicit expiry")
        if environment and set(environment) - {"python_version", "platform", "engine_version"}:
            raise WeatherError("Only allowlisted environment fingerprints may be stored")
        if any(not isinstance(x, str) for x in command):
            raise WeatherError("Command fingerprint requires an argument array")
        data = {"files": [file_fingerprint(p, (line_ranges or {}).get(str(p))) for p in files],
                "dependencies": [file_fingerprint(p) for p in dependencies or []],
                "command": command, "config": config, "environment": environment or {}, "external": external}
        key = digest(canonical_json(data))
        row = {**data, "conclusion": conclusion, "observed_at": utc_now(), "observed_epoch": time.time(), "expiry_seconds": expiry_seconds, "invalidated": None}
        from .storage import lock
        with lock(self.path.parent / "cache.lock"):
            rows = read_json(self.path, {})
            rows[key] = row
            atomic_json(self.path, rows)
        return key

    def verify(self, key: str, command: list[str] | None = None, config: dict | None = None,
               environment: dict | None = None) -> dict:
        row = read_json(self.path, {}).get(key)
        if row is None:
            return {"valid": False, "reason": "missing evidence"}
        if row["invalidated"]:
            return {"valid": False, "reason": row["invalidated"]}
        expiry = row["expiry_seconds"]
        if expiry is not None and time.time() - row["observed_epoch"] > expiry:
            return {"valid": False, "reason": "expired external observation"}
        for name, expected in (("command", command), ("config", config), ("environment", environment)):
            if expected is not None and row[name] != expected:
                return {"valid": False, "reason": f"changed {name}"}
        for entry in row["files"] + row["dependencies"]:
            try:
                current = file_fingerprint(Path(entry["path"]), tuple(entry["range"]) if entry["range"] else None)
            except (OSError, WeatherError):
                return {"valid": False, "reason": f'missing/unreadable {entry["path"]}'}
            if current != entry:
                return {"valid": False, "reason": f'changed {entry["path"]}'}
        return {"valid": True, "reused": True, "observed_at": row["observed_at"], "conclusion": row["conclusion"]}

    def invalidate(self, paths: list[Path], reason: str) -> list[str]:
        from .storage import lock
        affected = {str(p.resolve()) for p in paths}
        keys = []
        with lock(self.path.parent / "cache.lock"):
            rows = read_json(self.path, {})
            for key, row in rows.items():
                if affected & {r["path"] for r in row["files"] + row["dependencies"]}:
                    row["invalidated"] = reason
                    keys.append(key)
            atomic_json(self.path, rows)
        return keys
