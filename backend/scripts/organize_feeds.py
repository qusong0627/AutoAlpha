"""整理 Huntly 分类: 把未分组订阅源按规则移动到目标分类。

注意: Huntly 的 updateSetting 是全量覆盖更新（缺失字段会被写为 NULL）。
connectorItems 里不含 subscribeUrl，因此移动前必须先读取该源当前设置，
合并出完整字段再提交，否则会把 subscribe_url/is_enabled 抹成 NULL。

用法 (在 quantmind 容器内): python -m backend.scripts.organize_feeds
"""

import asyncio
from backend.services.api.routers.news import _huntly_request, _unwrap

DEFAULT_FOLDER = "A股快讯"

FOLDER_RULES = [
    (lambda name: "qlib" in name or "arxiv" in name, "量化研究"),
    (lambda name: "监管" in name or "政策" in name or "财新" in name, "宏观与监管"),
    (lambda name: "36氪" in name or "36kr" in name, "商业科技"),
]


def pick_folder(name: str) -> str:
    for rule, folder in FOLDER_RULES:
        if rule(name):
            return folder
    return DEFAULT_FOLDER


async def get_current_setting(connector_id: int) -> dict:
    """读取单个源当前设置 (updateSetting 全量覆盖前必须先取全字段)。"""
    r = await _huntly_request(
        "GET", "/api/setting/feeds/setting", params={"connectorId": connector_id})
    if r.status_code != 200:
        raise RuntimeError(f"读取设置失败 HTTP {r.status_code}: {r.text[:150]}")
    data = _unwrap(r.json()) or {}
    if not isinstance(data, dict):
        raise RuntimeError(f"读取设置响应异常: {str(data)[:150]}")
    return data


async def organize():
    print("=== 整理 Huntly 分类与订阅源 ===")

    r = await _huntly_request("GET", "/api/setting/folder/all")
    if r.status_code != 200:
        raise RuntimeError(f"获取分类失败 HTTP {r.status_code}: {r.text[:150]}")
    folder_map = {}
    for f in _unwrap(r.json()) or []:
        if isinstance(f, dict) and f.get("id") is not None and f.get("name"):
            folder_map[f["name"]] = f["id"]
    print(f"现有分类: {folder_map}")

    r = await _huntly_request("GET", "/api/connector/folder-connectors")
    if r.status_code != 200:
        raise RuntimeError(f"获取订阅源失败 HTTP {r.status_code}: {r.text[:150]}")
    body = _unwrap(r.json()) or {}
    ffc = body.get("folderFeedConnectors") if isinstance(body, dict) else body

    moved = failed = 0
    for f in ffc or []:
        # 仅处理未分组桶 (id=None)
        if f.get("id") is not None:
            continue
        for item in f.get("connectorItems") or []:
            cid = item.get("id")
            name = item.get("name") or ""
            target_folder = pick_folder(name)
            target_fid = folder_map.get(target_folder)
            if not cid or not target_fid:
                print(f"  ⚠ 跳过 {name!r} (缺少源 ID 或目标分类不存在)")
                continue
            try:
                current = await get_current_setting(cid)
                u = await _huntly_request(
                    "POST", "/api/setting/feeds/updateSetting",
                    json={
                        "connectorId": cid,
                        "subscribeUrl": current.get("subscribeUrl"),
                        "enabled": bool(current.get("enabled")),
                        "crawlFullContent": bool(current.get("crawlFullContent")),
                        "name": current.get("name") or name,
                        "fetchIntervalMinutes": current.get("fetchIntervalMinutes")
                        or current.get("defaultFetchIntervalMinutes"),
                        "folderId": target_fid,
                    })
                if u.status_code != 200:
                    raise RuntimeError(f"updateSetting HTTP {u.status_code}")
                print(f"  ✓ 移动「{name}」(ID: {cid}) → 分类「{target_folder}」")
                moved += 1
            except Exception as exc:
                print(f"  ✗ 移动「{name}」失败: {exc}")
                failed += 1
    print(f"=== 整理完成: 移动 {moved} 个, 失败 {failed} 个 ===")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(organize())
