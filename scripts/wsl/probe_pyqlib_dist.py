#!/usr/bin/env python3
"""列出 PyPI 上 pyqlib 的可用分发（判断 cp314 是否有 wheel）。"""
import json
import sys
import urllib.request

url = "https://pypi.org/pypi/pyqlib/json"
try:
    with urllib.request.urlopen(url, timeout=60) as r:
        d = json.load(r)
except Exception as e:  # noqa: BLE001
    print("FETCH_FAILED:", type(e).__name__, e)
    sys.exit(1)

info = d["info"]
print("latest version :", info["version"])
print("requires_python:", info.get("requires_python"))
print("summary        :", (info.get("summary") or "")[:80])

rels = d["releases"]
versions = sorted(rels)
print("\n版本数:", len(versions), "最近:", versions[-5:])

for v in versions[-4:]:
    files = rels[v]
    print(f"\n--- {v} ({len(files)} files) ---")
    for f in files:
        print("   ", f["filename"])

allnames = [f["filename"] for v in versions for f in rels[v]]
cp = sorted({n.split("-")[2] for n in allnames if n.endswith(".whl") and len(n.split("-")) > 2})
print("\nwheel python tags:", cp)
print("有 sdist:", any(f["packagetype"] == "sdist" for v in versions for f in rels[v]))
