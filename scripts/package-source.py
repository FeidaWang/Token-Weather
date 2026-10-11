#!/usr/bin/env python3
"""Create a portable source ZIP, excluding personal configuration and caches."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = json.loads((root / "plugin.json").read_text())
    ignored = {".git", ".codex", ".codex-plugin", ".env", ".venv", "__pycache__", "node_modules", "evidence", "hooks"}
    destination = args.output.absolute()
    if destination.is_symlink() or any(part.is_symlink() for part in destination.parents):
        parser.error("Output must not use a symlink")
    if destination.exists():
        parser.error("Output exists; select a new filename")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if path.is_symlink():
                raise ValueError("Source contains a symlink")
            if not path.is_file() or any(part in ignored for part in relative.parts) or path.suffix in (".pyc", ".zip", ".pem", ".key", ".p12") or relative.name.startswith(".env"):
                continue
            archive.write(path, manifest["name"] + "/" + relative.as_posix())
    print(json.dumps({"archive": str(destination), "version": manifest["version"],
                      "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(), "public_directory_ready": False}))


if __name__ == "__main__":
    main()
