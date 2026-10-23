#!/usr/bin/env python3
"""Validate AgenticSRE's embedded persistent database and JSON state.

The current deployment uses ChromaDB, whose local metadata is backed by
SQLite, plus JSON state files.  This command is deliberately read-only so it
can be used by the offline installer before backup, after restore, and during
acceptance checks.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def validate_tree(root: Path, *, allow_missing: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "ok",
        "root": str(root),
        "sqlite_files": 0,
        "json_files": 0,
        "errors": [],
    }
    if not root.exists():
        if allow_missing:
            result["status"] = "empty"
            return result
        result["status"] = "error"
        result["errors"].append(f"persistent data directory does not exist: {root}")
        return result

    sqlite_paths = sorted(
        path
        for pattern in ("*.sqlite", "*.sqlite3", "*.db")
        for path in root.rglob(pattern)
        if path.is_file()
    )
    # A file matching two patterns must only be checked once.
    sqlite_paths = list(dict.fromkeys(sqlite_paths))
    result["sqlite_files"] = len(sqlite_paths)
    for path in sqlite_paths:
        try:
            uri = f"file:{path.resolve()}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
            if not row or str(row[0]).lower() != "ok":
                result["errors"].append(f"SQLite quick_check failed: {path}: {row}")
        except Exception as exc:  # pragma: no cover - exact sqlite error varies
            result["errors"].append(f"SQLite cannot be read: {path}: {exc}")

    json_paths = sorted(path for path in root.rglob("*.json") if path.is_file())
    result["json_files"] = len(json_paths)
    for path in json_paths:
        try:
            with path.open("r", encoding="utf-8") as handle:
                json.load(handle)
        except Exception as exc:
            result["errors"].append(f"JSON cannot be decoded: {path}: {exc}")

    if result["errors"]:
        result["status"] = "error"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only integrity check for AgenticSRE persistent data"
    )
    parser.add_argument("--path", type=Path, default=Path("/app/data"))
    parser.add_argument("--allow-missing", action="store_true")
    args = parser.parse_args()
    result = validate_tree(args.path, allow_missing=args.allow_missing)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] in {"ok", "empty"} else 1


if __name__ == "__main__":
    sys.exit(main())
