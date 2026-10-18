#!/usr/bin/env python3
"""Rebuild the read-only upstream mirror divergence inventory.

The upstream checkout is queried only through ``git ls-tree``. This script reads
the local mirror and writes ``MIRROR_DIFFS.md`` in the current repository.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import subprocess
from pathlib import Path, PurePosixPath


SCOPES = (
    "backend",
    "config",
    "docs",
    "alphaagent",
    "requirements",
    "scripts",
)
ROOT_FILES = (
    ".npmrc",
    "AGENTS.md",
    "README.md",
    "pyproject.toml",
    "run-wsl.sh",
    "requirements-factor-mining.txt",
    "requirements-factor-mining.lock",
)
EXCLUDED_DIRS = {"__pycache__", "node_modules", "dist"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo"}


def blob_oid(data: bytes) -> str:
    header = b"blob " + str(len(data)).encode("ascii") + b"\0"
    return hashlib.sha1(header + data).hexdigest()


def is_generated(path: str) -> bool:
    relative = PurePosixPath(path)
    return (
        any(part in EXCLUDED_DIRS for part in relative.parts)
        or relative.suffix in EXCLUDED_SUFFIXES
        or path == "config/runtime.env"
        or relative.name.startswith(".env.")
    )


def upstream_blobs(upstream: Path) -> dict[str, str]:
    raw = subprocess.check_output(
        [
            "git",
            "-C",
            str(upstream),
            "ls-tree",
            "-rz",
            "--full-tree",
            "next",
            "--",
            *SCOPES,
            *ROOT_FILES,
        ]
    )
    result: dict[str, str] = {}
    for row in raw.split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        _mode, _kind, oid = metadata.decode("ascii").split()
        path = raw_path.decode("utf-8", "surrogateescape")
        result[path] = oid
    return result


def local_blobs(repo: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for scope in SCOPES:
        root = repo / scope
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(repo).as_posix()
            if not is_generated(relative):
                result[relative] = blob_oid(path.read_bytes())

    for name in ROOT_FILES:
        path = repo / name
        if path.is_file() and not is_generated(name):
            result[name] = blob_oid(path.read_bytes())
    return result


def visible_path(path: str) -> str:
    """Render undecodable Git path bytes safely in a UTF-8 Markdown report."""
    return path.encode("utf-8", "backslashreplace").decode("utf-8")


def build_report(repo: Path, upstream: Path) -> str:
    reviewed_section = ""
    existing_report = repo / "MIRROR_DIFFS.md"
    if existing_report.is_file():
        previous = existing_report.read_text(encoding="utf-8")
        reviewed_marker = "## Reviewed intentional content divergences"
        newline_marker = "## Newline-only differences"
        if reviewed_marker in previous and newline_marker in previous:
            reviewed_section = previous.split(reviewed_marker, 1)[1].split(
                newline_marker, 1
            )[0].strip()

    remote = upstream_blobs(upstream)
    local = local_blobs(repo)
    common = set(remote) & set(local)
    differing = sorted(path for path in common if remote[path] != local[path])
    newline_only = []
    content_different = []
    for relative in differing:
        upstream_data = subprocess.check_output(
            ["git", "-C", str(upstream), "show", f"next:{relative}"]
        )
        local_data = (repo / Path(relative)).read_bytes()
        if upstream_data.replace(b"\r\n", b"\n") == local_data.replace(
            b"\r\n", b"\n"
        ):
            newline_only.append(relative)
        else:
            content_different.append(relative)

    mirror_only = sorted(set(local) - set(remote))
    upstream_only = sorted(set(remote) - set(local))
    omitted_counts = collections.Counter(
        PurePosixPath(path).parts[0]
        if len(PurePosixPath(path).parts) > 1
        else PurePosixPath(path).name + "/"
        for path in upstream_only
    )

    lines = [
        "# Mirror divergence inventory",
        "",
        "Compared local file contents with the committed `next` tree using Git blob SHA-1 object IDs. The upstream checkout is queried read-only with `git ls-tree` and `git show`.",
        "",
        "Scope: `backend/`, `config/`, `docs/`, `alphaagent/`, `requirements/`, `scripts/`, plus `.npmrc`, `AGENTS.md`, `README.md`, `pyproject.toml`, `run-wsl.sh`, and root factor-mining requirements files. Excluded generated `__pycache__` / `.pyc`, `node_modules`, `dist`, and runtime env files.",
        "",
        f"Within this scope: **{len(differing)} common files differ at the byte level**; after CRLF/LF normalization, **{len(newline_only)} are newline-only differences and {len(content_different)} still differ in content**. There are **{len(mirror_only)} mirror-only files** and **{len(upstream_only)} upstream-only files**. `web/` is a purpose-built local app and is not byte-compared. The upstream `electron/` app is intentionally omitted per `AGENTS.md`.",
        "",
        "## Common files with content differences",
        "",
    ]
    lines.extend(
        f"- `{visible_path(path)}`" for path in content_different or ["None"]
    )
    if reviewed_section:
        lines.extend(
            [
                "",
                "## Reviewed intentional content divergences",
                "",
                *reviewed_section.splitlines(),
            ]
        )
    lines.extend(["", "## Newline-only differences", ""])
    lines.extend(
        f"- `{visible_path(path)}`" for path in newline_only or ["None"]
    )
    lines.extend(["", "## Mirror-only files", ""])
    lines.extend(f"- `{visible_path(path)}`" for path in mirror_only or ["None"])
    lines.extend(["", "## Upstream-only counts by directory", ""])
    lines.extend(
        f"- `{visible_path(directory)}`: {count} files omitted from the slim mirror"
        for directory, count in sorted(omitted_counts.items())
    )
    lines.extend(
        [
            "",
            "This inventory is a safety reference, not a synchronization instruction. Do not overwrite from `next` without reviewing every listed divergence. Rebuild it after future mirror or source changes with `python scripts/mirror-diff-inventory.py --upstream D:/git/quantmindoss`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream",
        type=Path,
        default=Path.cwd().parent / "quantmindoss",
        help="read-only upstream checkout containing branch next",
    )
    parser.add_argument(
        "--repo", type=Path, default=Path.cwd(), help="local mirror root"
    )
    args = parser.parse_args()
    repo = args.repo.resolve()
    upstream = args.upstream.resolve()
    report = build_report(repo, upstream)
    (repo / "MIRROR_DIFFS.md").write_text(report, encoding="utf-8", newline="\n")
    first = report.splitlines()[6]
    print(first)
    print(f"Wrote {repo / 'MIRROR_DIFFS.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
