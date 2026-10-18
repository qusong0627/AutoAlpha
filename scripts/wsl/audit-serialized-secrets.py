#!/usr/bin/env python3
"""Read-only byte scan for credential indicators in RD-Agent serialized artifacts.

This deliberately does not deserialize pickle/joblib files. It reports paths and
indicator classes only; matched bytes are never printed.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


EXTENSIONS = {".pkl", ".pickle", ".joblib"}
CATEGORIES = {
    "credential-field": (
        r"api[_ -]?key|secret[_ -]?(key|id)?|access[_ -]?token|refresh[_ -]?token|"
        r"authorization|bearer|openai[_ -]?api[_ -]?key|deepseek[_ -]?api[_ -]?key|"
        r"mineru[_ -]?api[_ -]?key"
    ),
    "known-key-format": (
        r"sk-[A-Za-z0-9_-]{16,}|AKIA[A-Z0-9]{16}|ASIA[A-Z0-9]{16}|"
        r"AIza[0-9A-Za-z_-]{30,}|Bearer[[:space:]]+[A-Za-z0-9._~+/-]{16,}"
    ),
    "credential-assignment": (
        r"(api[_ -]?key|secret|token|password)[[:space:]]*[=:][[:space:]]*"
        r"['\"]?[A-Za-z0-9_./+=:-]{16,}"
    ),
    "litellm-setting-marker": r"litellm[_ -]?settings",
}
INDICATOR_PATTERN = "|".join(f"({pattern})" for pattern in CATEGORIES.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root",
        nargs="?",
        type=Path,
        default=Path("/data/alpha_agent_logs"),
        help="artifact root (default: /data/alpha_agent_logs)",
    )
    args = parser.parse_args()
    root = args.root
    if not root.is_dir():
        print(f"ERROR: artifact root is not a directory: {root}", file=sys.stderr)
        return 2

    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.suffix.lower() in EXTENSIONS
    )
    if not paths:
        print("Scanned 0 files; 0 files had credential indicators.")
        return 0
    try:
        result = subprocess.run(
            ["grep", "-a", "-l", "-Z", "-E", "-i", "--", INDICATOR_PATTERN,
             *(str(path) for path in paths)],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        print(f"ERROR: could not run grep: {type(exc).__name__}", file=sys.stderr)
        return 2
    matched = [Path(value.decode("utf-8", errors="replace")) for value in result.stdout.split(b"\0") if value]
    for path in matched:
        categories = []
        for name, pattern in CATEGORIES.items():
            check = subprocess.run(
                ["grep", "-a", "-q", "-E", "-i", "--", pattern, str(path)],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if check.returncode == 0:
                categories.append(name)
        print(f"MATCH {path.relative_to(root)} [{', '.join(categories)}]")
    if result.returncode not in (0, 1):
        print(f"ERROR: grep failed with exit code {result.returncode}", file=sys.stderr)
        return 2
    print(f"Scanned {len(paths)} files; {len(matched)} files had credential indicators.")
    print("No matched bytes or credential values were printed.")
    return 1 if matched else 0


if __name__ == "__main__":
    raise SystemExit(main())
