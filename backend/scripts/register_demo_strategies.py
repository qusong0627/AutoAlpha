"""把 strategy_templates/ 下全部演示模板注册为指定用户的个人策略。

幂等：已存在同名或同 template:<id> 标签的跳过，可重复执行。
模板目录走 StrategyTemplateLoader 单一事实源（与 sys_<id> 同源）。

用法（服务器上，项目根目录）：
    docker exec quantmind python3 /app/backend/scripts/register_demo_strategies.py
    docker exec quantmind python3 /app/backend/scripts/register_demo_strategies.py --user admin --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import sys


def _build_metadata(template) -> dict:
    params = {p.name: p.default for p in (template.params or [])}
    parameters = {"strategy_type": template.id, "signal": "<PRED>", **params}
    tags = ["demo", "template", str(template.category or "basic"), f"template:{template.id}"]
    return {
        "description": template.description or template.name,
        "strategy_type": "CUSTOM",
        "status": "ACTIVE",
        "parameters": parameters,
        "config": {
            "difficulty": template.difficulty,
            "execution_defaults": dict(template.execution_defaults or {}),
            "live_defaults": dict(template.live_defaults or {}),
        },
        "execution_config": {"max_buy_drop": -0.03},
        "tags": tags,
    }


async def main() -> int:
    ap = argparse.ArgumentParser(description="注册演示策略到个人策略中心")
    ap.add_argument("--user", default="00000001", help="目标用户 user_id（默认 admin 的 00000001）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    args = ap.parse_args()

    from backend.services.engine.qlib_app.services.strategy_templates import (
        get_all_templates,
    )
    from backend.shared.strategy_storage import get_strategy_storage_service

    templates = get_all_templates()
    if not templates:
        print("模板目录为空，未做任何事")
        return 1
    print(f"模板总数: {len(templates)}")

    storage = get_strategy_storage_service()
    existing = storage.list(args.user)
    existing_tags: set[str] = set()
    existing_names: set[str] = set()
    for item in existing:
        existing_names.add(str(item.get("name") or ""))
        for t in item.get("tags") or []:
            existing_tags.add(str(t))

    created, skipped, failed = 0, 0, []
    for t in templates:
        marker = f"template:{t.id}"
        if marker in existing_tags or t.name in existing_names:
            skipped += 1
            continue
        if args.dry_run:
            print(f"[dry-run] 将创建: {t.name} ({t.id})")
            created += 1
            continue
        try:
            await storage.save(
                user_id=args.user,
                name=t.name,
                code=t.code,
                metadata=_build_metadata(t),
            )
            created += 1
        except Exception as exc:
            failed.append((t.id, str(exc)[:200]))
            print(f"[失败] {t.id}: {exc}", file=sys.stderr)

    print(f"完成: 新建 {created} / 跳过 {skipped} / 失败 {len(failed)}")
    for tid, err in failed:
        print(f"  - {tid}: {err}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
