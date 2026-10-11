#!/usr/bin/env python3
"""Portable weather entry point; bootstrap macOS's older system Python safely."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    if sys.version_info < (3, 11):
        candidates = [os.environ.get("TOKEN_WEATHER_PYTHON")]
        try:
            candidates.append(json.loads((ROOT / "runtime-pin.json").read_text()).get("python"))
        except (OSError, ValueError):
            pass
        candidates += [shutil.which(name) for name in ("python3.13", "python3.12", "python3.11")]
        for path in candidates:
            if not path or not Path(path).is_file() or Path(path).resolve() == Path(sys.executable).resolve():
                continue
            try:
                check = subprocess.run([path, "-c", "import sys;sys.exit(sys.version_info < (3,11))"],
                                       capture_output=True, timeout=2)
                if check.returncode == 0:
                    os.execv(path, [path, str(Path(__file__).resolve()), *sys.argv[1:]])
            except (OSError, subprocess.TimeoutExpired):
                continue
        print("Token Weather requires Python 3.11+; set TOKEN_WEATHER_PYTHON to an installed interpreter.", file=sys.stderr)
        if "weather-hook" in sys.argv:
            print("{}")
            return 0
        return 2
    sys.path.insert(0, str(ROOT / "src"))
    from token_weather.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
