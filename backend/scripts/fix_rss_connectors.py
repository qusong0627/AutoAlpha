"""修复 Huntly 订阅源存量脏数据。

背景: 旧版 admin_create_source 在 follow 后追加 updateSetting 时只传了部分字段，
而 Huntly 的 updateSetting 是全量覆盖更新，导致 connector 表中 subscribe_url /
is_enabled 被抹成 NULL。后果:
  1) /api/connector/folder-connectors 只返回 is_enabled=true 的源 → 前端左侧树缺源;
  2) follow 的按 URL 去重失效 → 同一源被重复创建。

本脚本 (幂等，可重复执行):
  1. 只读扫描 Huntly SQLite，找出所有 connector 及其归属;
  2. 按预设清单还原丢失的 subscribe_url，按 URL 分组去重(保留文章最多的一条);
  3. 通过 Huntly API 补建缺失的分类目录;
  4. 用全量字段的 updateSetting 修复保留源 (subscribeUrl + enabled + 分类);
  5. 打印修复前后对比。

用法 (在 quantmind 容器内): python -m backend.scripts.fix_rss_connectors
"""

import asyncio
import sqlite3
from pathlib import Path

from backend.services.api.routers.news import _huntly_request, _unwrap

# 预设订阅源清单 (与前端 AdminRssSources PRESET_FEEDS 保持一致)
KNOWN_FEEDS = [
    {"keys": ["同花顺", "10jqka"], "url": "http://quantmind-rsshub:1200/10jqka/realtimenews",
     "name": "同花顺 7x24直播", "folder": "A股快讯"},
    {"keys": ["财联社", "cls/telegraph"], "url": "http://quantmind-rsshub:1200/cls/telegraph",
     "name": "财联社 7x24快讯", "folder": "A股快讯"},
    {"keys": ["华尔街见闻", "wallstreetcn"], "url": "http://quantmind-rsshub:1200/wallstreetcn/news/global",
     "name": "华尔街见闻 实时快讯", "folder": "A股快讯"},
    {"keys": ["格隆汇", "gelonghui"], "url": "http://quantmind-rsshub:1200/gelonghui/live",
     "name": "格隆汇 实时快讯", "folder": "A股快讯"},
    {"keys": ["金十", "jin10"], "url": "http://quantmind-rsshub:1200/jin10/news",
     "name": "金十数据 实时快讯", "folder": "A股快讯"},
    {"keys": ["财新", "caixin"], "url": "http://quantmind-rsshub:1200/caixin/finance/regulation",
     "name": "财新网 金融监管", "folder": "宏观与监管"},
    {"keys": ["36氪", "36kr"], "url": "http://quantmind-rsshub:1200/36kr/newsflashes",
     "name": "36氪 商业快讯", "folder": "商业科技"},
    {"keys": ["arxiv.org/rss/q-fin", "arxiv 计算机金融"], "url": "http://export.arxiv.org/rss/q-fin",
     "name": "arXiv q-fin 预印本", "folder": "量化研究"},
    {"keys": ["qlib/releases.atom", "qlib 官方"], "url": "https://github.com/microsoft/qlib/releases.atom",
     "name": "Microsoft Qlib 官方更新", "folder": "量化研究"},
]


def find_huntly_db() -> Path | None:
    candidates = []
    if os_env := __import__("os").getenv("HUNTLY_DB_PATH"):
        candidates.append(Path(os_env))
    candidates += [
        Path("/data/huntly/db.sqlite"),
        Path(__file__).resolve().parents[2] / "data" / "huntly" / "db.sqlite",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def match_known(name: str | None, url: str | None) -> dict | None:
    for feed in KNOWN_FEEDS:
        if url and feed["url"] == url:
            return feed
        if name and any(k.lower() in name.lower() for k in feed["keys"]):
            return feed
    return None


async def ensure_folders() -> dict[str, int]:
    """补建缺失分类，返回 名称->id 映射 (失败直接抛错，不再静默)。"""
    r = await _huntly_request("GET", "/api/setting/folder/all")
    if r.status_code != 200:
        raise RuntimeError(f"Huntly folder/all HTTP {r.status_code}: {r.text[:200]}")
    folder_map = {}
    for f in _unwrap(r.json()) or []:
        if isinstance(f, dict) and f.get("id") is not None and f.get("name"):
            folder_map[f["name"]] = f["id"]

    wanted = sorted({feed["folder"] for feed in KNOWN_FEEDS})
    for name in wanted:
        if name in folder_map:
            continue
        r = await _huntly_request("POST", "/api/setting/folder/save", json={"name": name})
        if r.status_code != 200:
            raise RuntimeError(f"创建分类「{name}」失败 HTTP {r.status_code}: {r.text[:200]}")
        created = _unwrap(r.json()) or {}
        if not isinstance(created, dict) or created.get("id") is None:
            raise RuntimeError(f"创建分类「{name}」响应缺少 id: {r.text[:200]}")
        folder_map[name] = created["id"]
        print(f"  ✓ 创建分类「{name}」(ID: {created['id']})")
    return folder_map


async def fix_connector(connector_id: int, *, url: str, name: str,
                        folder_id: int | None) -> None:
    """全量字段覆盖式修复 (缺失字段会被 Huntly 写 NULL，必须带全)。"""
    body: dict = {
        "connectorId": connector_id,
        "subscribeUrl": url,
        "enabled": True,
        "crawlFullContent": False,
    }
    if name:
        body["name"] = name
    if folder_id is not None:
        body["folderId"] = folder_id
    r = await _huntly_request("POST", "/api/setting/feeds/updateSetting", json=body)
    if r.status_code != 200:
        raise RuntimeError(
            f"修复源 #{connector_id} 失败 HTTP {r.status_code}: {r.text[:200]}")


async def main():
    db_path = find_huntly_db()
    if not db_path:
        raise SystemExit("未找到 Huntly 数据库，请用 HUNTLY_DB_PATH 环境变量指定路径")

    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "SELECT id, name, subscribe_url, is_enabled, inbox_count FROM connector ORDER BY id")]
    con.close()

    print(f"=== 扫描到 {len(rows)} 个 connector ({db_path}) ===")
    for row in rows:
        flag = "OK" if (row["is_enabled"] and row["subscribe_url"]) else "BROKEN"
        print(f"  [{flag}] #{row['id']} {row['name']!r} "
              f"url={row['subscribe_url']!r} enabled={row['is_enabled']} "
              f"inbox={row['inbox_count']}")

    # 按 URL 分组 (URL 缺失时用预设清单还原)
    groups: dict[str, list[dict]] = {}
    unresolved: list[dict] = []
    for row in rows:
        feed = match_known(row["name"], row["subscribe_url"])
        if feed is None:
            unresolved.append(row)
            continue
        groups.setdefault(feed["url"], []).append({**row, "_feed": feed})

    # 去重: 每个 URL 保留一条 (优先已启用且 URL 完整 > inbox 多 > id 小)
    keepers: list[dict] = []
    to_delete: list[dict] = []
    for url, items in groups.items():
        items.sort(key=lambda x: (
            not bool(x["is_enabled"] and x["subscribe_url"]),
            -(x["inbox_count"] or 0),
            x["id"],
        ))
        keepers.append(items[0])
        to_delete.extend(items[1:])

    deleted, fixed = 0, 0
    for row in to_delete:
        r = await _huntly_request(
            "POST", "/api/setting/feeds/delete", params={"connectorId": row["id"]})
        if r.status_code == 200:
            deleted += 1
            print(f"  ✗ 删除重复源 #{row['id']} {row['name']!r}")
        else:
            print(f"  ⚠ 删除重复源 #{row['id']} 失败 HTTP {r.status_code}")

    folder_map = await ensure_folders()

    print("=== 修复保留源 (全量字段覆盖) ===")
    for row in keepers:
        feed = row["_feed"]
        need_fix = not (row["is_enabled"] and row["subscribe_url"])
        try:
            await fix_connector(
                row["id"], url=feed["url"], name=feed["name"],
                folder_id=folder_map.get(feed["folder"]))
            tag = "FIXED" if need_fix else "ok"
            fixed += 1 if need_fix else 0
            print(f"  [{tag}] #{row['id']} {feed['name']} → 分类「{feed['folder']}」")
        except Exception as exc:
            print(f"  ✗ 修复 #{row['id']} 失败: {exc}")

    for row in unresolved:
        print(f"  ⚠ 无法识别的源 #{row['id']} {row['name']!r} "
              f"(url={row['subscribe_url']!r}), 已跳过, 请手动处理")

    r = await _huntly_request("GET", "/api/connector/folder-connectors")
    body = _unwrap(r.json()) or {}
    ffc = body.get("folderFeedConnectors") if isinstance(body, dict) else body
    visible = sum(len(f.get("connectorItems") or []) for f in (ffc or []))
    print(f"=== 完成: 删除重复 {deleted} 个, 修复不可见源 {fixed} 个 ===")
    print(f"=== 左侧树当前可见源数 (folder-connectors): {visible} ===")


if __name__ == "__main__":
    asyncio.run(main())
