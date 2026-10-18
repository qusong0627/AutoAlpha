#!/usr/bin/env python3
"""独立挂载自检：只看路由挂载结果，不碰数据库。

用法（WSL）：
    /home/qusong/autoalpha-venv/bin/python /mnt/d/git/autoalpha/scripts/wsl/verify_mounts.py
"""
import json
import sys

sys.path.insert(0, "/mnt/d/git/autoalpha")

from fastapi.testclient import TestClient  # noqa: E402

from backend.services.engine.app_factor_mining import app  # noqa: E402


def route_path(r) -> str | None:
    """兼容本版 FastAPI 的 _EffectiveRouteContext 包装体。"""
    for attr in ("path", "path_format"):
        v = getattr(r, attr, None)
        if isinstance(v, str) and v:
            return v
    return None


def route_methods(r):
    m = getattr(r, "methods", None)
    if m:
        return set(m)
    inner = getattr(r, "original_route", None)
    return set(getattr(inner, "methods", None) or set())


client = TestClient(app)
h = client.get("/health")
payload = h.json()
print("HTTP", h.status_code)
print(json.dumps({k: payload[k] for k in ("status", "failed")}, indent=2, ensure_ascii=False))
for line in payload["mounted"]:
    print("  mounted:", line)

total = 0
for line in payload["mounted"]:
    try:
        total += int(line.split("(")[1].split()[0])
    except (IndexError, ValueError):
        pass
print(f"\ntotal routes mounted: {total}")

probe = client.get("/api/v1/alpha-agent/markets")
print(f"\nunauth probe /api/v1/alpha-agent/markets -> {probe.status_code}")
print("AUTH_GATE_OK" if probe.status_code == 401 else "AUTH_GATE_UNEXPECTED")
print("ALL_MOUNTED_OK" if not payload["failed"] else "SOME_MOUNT_FAILED")
