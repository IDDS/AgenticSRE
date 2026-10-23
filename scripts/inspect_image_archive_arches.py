#!/usr/bin/env python3
"""Inspect docker-save image archives and verify expected architectures."""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import tarfile
from pathlib import Path


def _open_tar(path: Path) -> tarfile.TarFile:
    if path.suffix in {".gz", ".tgz"}:
        return tarfile.open(path, "r:gz")
    return tarfile.open(path, "r:")


def inspect_archive(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    with _open_tar(path) as tf:
        manifest_member = tf.extractfile("manifest.json")
        if manifest_member is None:
            raise ValueError(f"{path}: manifest.json not found; expected docker save archive")
        manifest = json.load(manifest_member)
        for entry in manifest:
            config_name = entry.get("Config")
            if not config_name:
                continue
            config_member = tf.extractfile(config_name)
            if config_member is None:
                continue
            config = json.load(config_member)
            tags = entry.get("RepoTags") or ["<none>"]
            for tag in tags:
                rows.append({
                    "archive": str(path),
                    "image": tag,
                    "architecture": str(config.get("architecture", "")),
                    "os": str(config.get("os", "")),
                    "variant": str(config.get("variant", "")),
                })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archives", nargs="+", help="docker save .tar/.tar.gz archives")
    parser.add_argument("--require-arch", default="", help="required architecture, e.g. arm64 or amd64")
    parser.add_argument("--match", default="", help="only enforce --require-arch for image refs containing this text")
    args = parser.parse_args()

    failures: list[str] = []
    all_rows: list[dict[str, str]] = []
    for raw in args.archives:
        path = Path(raw)
        try:
            rows = inspect_archive(path)
        except (OSError, tarfile.TarError, gzip.BadGzipFile, json.JSONDecodeError, ValueError) as exc:
            failures.append(str(exc))
            continue
        all_rows.extend(rows)

    for row in all_rows:
        print("{archive}\t{image}\t{os}/{architecture}{variant}".format(**row))
        if args.require_arch:
            if args.match and args.match not in row["image"]:
                continue
            if row["architecture"] != args.require_arch:
                failures.append(
                    f"{row['image']} in {row['archive']} is {row['architecture']}, expected {args.require_arch}"
                )

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
