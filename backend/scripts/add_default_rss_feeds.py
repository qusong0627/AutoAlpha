"""通过 Huntly API 批量添加精选量化与财经 RSS 订阅源。

注意: Huntly 的 updateSetting 是全量覆盖更新（缺失字段会被写为 NULL），
所以设置名称/分类时必须显式携带 subscribeUrl 与 enabled，否则订阅会"隐形"。

用法 (在 quantmind 容器内): python -m backend.scripts.add_default_rss_feeds
"""

import asyncio
from backend.services.api.routers.news import _huntly_request, _unwrap

FOLDERS = ["A股快讯", "宏观与监管", "量化研究", "商业科技"]

FEEDS = [
    {"name": "同花顺 7x24直播", "url": "http://quantmind-rsshub:1200/10jqka/realtimenews", "folder": "A股快讯"},
    {"name": "财联社 7x24快讯", "url": "http://quantmind-rsshub:1200/cls/telegraph", "folder": "A股快讯"},
    {"name": "华尔街见闻 实时快讯", "url": "http://quantmind-rsshub:1200/wallstreetcn/news/global", "folder": "A股快讯"},
    {"name": "格隆汇 实时快讯", "url": "http://quantmind-rsshub:1200/gelonghui/live", "folder": "A股快讯"},
    {"name": "金十数据 实时快讯", "url": "http://quantmind-rsshub:1200/jin10/news", "folder": "A股快讯"},
    {"name": "财新网 金融监管", "url": "http://quantmind-rsshub:1200/caixin/finance/regulation", "folder": "宏观与监管"},
    {"name": "36氪 商业快讯", "url": "http://quantmind-rsshub:1200/36kr/newsflashes", "folder": "商业科技"},
    {"name": "arXiv 计算机金融预印本", "url": "http://export.arxiv.org/rss/q-fin", "folder": "量化研究"},
    {"name": "Microsoft Qlib 官方更新", "url": "https://github.com/microsoft/qlib/releases.atom", "folder": "量化研究"},
]


async def ensure_folders() -> dict[str, int]:
    """创建分类并返回 名称->id 映射，任何失败都显式报错。"""
    r = await _huntly_request("GET", "/api/setting/folder/all")
    if r.status_code != 200:
        raise RuntimeError(f"获取分类列表失败 HTTP {r.status_code}: {r.text[:200]}")
    folder_map = {}
    for f in _unwrap(r.json()) or []:
        if isinstance(f, dict) and f.get("id") is not None and f.get("name"):
            folder_map[f["name"]] = f["id"]

    for name in FOLDERS:
        if name in folder_map:
            continue
        r = await _huntly_request("POST", "/api/setting/folder/save", json={"name": name})
        if r.status_code != 200:
            raise RuntimeError(f"创建分类「{name}」失败 HTTP {r.status_code}: {r.text[:200]}")
        created = _unwrap(r.json()) or {}
        if not isinstance(created, dict) or created.get("id") is None:
            raise RuntimeError(f"创建分类「{name}」响应缺少 id: {r.text[:200]}")
        folder_map[name] = created["id"]
        print(f"✓ 创建分类「{name}」(ID: {created['id']})")
    print(f"分类映射表: {folder_map}")
    return folder_map


async def add_feeds():
    print("=== 开始批量添加精选 RSS 订阅源 ===")
    folder_map = await ensure_folders()

    ok = fail = skip = 0
    for feed in FEEDS:
        label = f"{feed['name']} ({feed['url']})"
        try:
            r = await _huntly_request(
                "POST", "/api/setting/feeds/follow",
                params={"subscribeUrl": feed["url"]})
            if r.status_code != 200:
                raise RuntimeError(f"follow HTTP {r.status_code}: {r.text[:150]}")
            follow_data = _unwrap(r.json()) or {}
            conn_id = follow_data.get("id") or follow_data.get("connectorId")
            if not conn_id:
                print(f"- 已存在或响应异常，跳过: {label} → {str(follow_data)[:150]}")
                skip += 1
                continue

            # 全量字段覆盖: 缺失字段会被 Huntly 置 NULL
            u = await _huntly_request(
                "POST", "/api/setting/feeds/updateSetting",
                json={
                    "connectorId": conn_id,
                    "subscribeUrl": feed["url"],
                    "enabled": True,
                    "crawlFullContent": False,
                    "name": feed["name"],
                    "folderId": folder_map.get(feed["folder"]),
                })
            if u.status_code != 200:
                raise RuntimeError(f"updateSetting HTTP {u.status_code}: {u.text[:150]}")
            print(f"  ✓ 已添加: {feed['name']} (ID: {conn_id}, 分类: {feed['folder']})")
            ok += 1
        except Exception as exc:
            print(f"  ✗ 添加失败 {label}: {exc}")
            fail += 1

    print(f"=== 批量添加完成: 成功 {ok}, 跳过 {skip}, 失败 {fail} ===")
    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(add_feeds())
