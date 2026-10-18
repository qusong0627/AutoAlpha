#!/usr/bin/env python3
"""修复广场导入模型的非 ASCII model_id（存量）。

背景：`hub_proxy._slugify_hub_name` 曾用 `str.isalnum()` 判字符——中文的
isalnum() 恒为 True 被原样保留 → 导入出的本地 model_id 形如
`mdl_cn_hub_T_10_LightGBM_选股_d899b0_0792`。该 ID 在滚动训练派生配方处被
`model_recipe._SAFE_MODEL_ID`（`[A-Za-z0-9._-]{1,128}`）拒绝：「非法 model_id」，
模型无法进入滚动训练。生成侧已在 hub_proxy 同日修复（仅 ASCII），本脚本修存量。

修复口径：与 hub_proxy 修正后的 ASCII 化同源——把 model_id 中非
`[A-Za-z0-9._-]` 的字符替换为下划线并压缩。中文展示名（metadata 的
display_name / hub_name）**不动**，那是设计内的展示层字段。

消费方两侧都改（缺一不可）：
  - DB `qm_user_models`：model_id + storage_path；
  - 磁盘模型目录：os.rename，与 storage_path 同步（模型加载端按目录读）。
写后回读复验；幂等：重复执行第二次为空操作。

用法（容器内）:
    python backend/scripts/repair_nonascii_user_model_ids.py            # dry-run 报表
    python backend/scripts/repair_nonascii_user_model_ids.py --apply    # 执行
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
sys.path.insert(0, PROJECT_ROOT)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("repair_nonascii_model_ids")

# 与 model_recipe._SAFE_MODEL_ID 同口径（滚动训练派生配方的校验器）
SAFE_MODEL_ID = re.compile(r"[A-Za-z0-9._-]{1,128}")

DEFAULT_MODELS_ROOT = Path(os.getenv("QM_MODELS_ROOT", "/app/models/users"))


def _get_engine():
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', '')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}"
        f"/{os.getenv('DB_NAME', 'quantmind')}",
    )
    # 容器 DATABASE_URL 是 postgresql+asyncpg://（服务运行用），本脚本同步执行，
    # 剥掉驱动后缀回退到同步 psycopg2。
    return create_engine(db_url.replace("+asyncpg", ""))


def _ascii_id(old: str) -> str:
    """非 [A-Za-z0-9._-] 字符 → 下划线；压缩连续下划线；截断到 128。"""
    s = "".join(
        c if (c.isascii() and (c.isalnum() or c in "._-")) else "_" for c in old
    )
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:128].strip("_")


def _scan(engine) -> list[dict]:
    with engine.connect() as conn:
        rows = (
            conn.execute(
                text(
                    """
                    SELECT tenant_id, user_id, model_id, storage_path, status
                    FROM qm_user_models
                    WHERE model_id <> ''
                    ORDER BY created_at
                    """
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows if not SAFE_MODEL_ID.fullmatch(str(r["model_id"]))]


def _plan_one(engine, row: dict) -> dict:
    old_id = str(row["model_id"])
    new_id = _ascii_id(old_id)
    issues: list[str] = []
    if not new_id:
        issues.append("ASCII 化后为空，需人工处理")
    elif not SAFE_MODEL_ID.fullmatch(new_id):
        issues.append(f"ASCII 化后仍非法: {new_id!r}")

    storage = str(row.get("storage_path") or "")
    old_dir = Path(storage) if storage else None
    new_dir = old_dir.parent / new_id if old_dir else None

    if storage and old_id not in storage:
        issues.append(f"storage_path 不含旧 ID: {storage!r}")
    if old_dir is not None and not old_dir.is_dir():
        issues.append(f"旧目录不存在: {old_dir}")
    if (
        new_dir is not None
        and old_dir is not None
        and new_dir != old_dir
        and new_dir.exists()
    ):
        issues.append(f"目标目录已存在: {new_dir}")

    if new_id and not issues:
        with engine.connect() as conn:
            clash = conn.execute(
                text(
                    """
                    SELECT count(*) FROM qm_user_models
                    WHERE tenant_id = :t AND user_id = :u
                      AND model_id = :m AND model_id <> :old
                    """
                ),
                {
                    "t": row["tenant_id"],
                    "u": row["user_id"],
                    "m": new_id,
                    "old": old_id,
                },
            ).scalar()
        if clash:
            issues.append(f"新 ID 与既有记录冲突: {new_id}")

    return {
        "tenant_id": row["tenant_id"],
        "user_id": row["user_id"],
        "old_id": old_id,
        "new_id": new_id,
        "old_storage": storage,
        "new_storage": storage.replace(old_id, new_id) if storage else "",
        "old_dir": str(old_dir) if old_dir else "",
        "new_dir": str(new_dir) if new_dir else "",
        "issues": issues,
    }


def _apply_one(engine, plan: dict) -> None:
    old_dir = Path(plan["old_dir"]) if plan["old_dir"] else None
    new_dir = Path(plan["new_dir"]) if plan["new_dir"] else None
    if old_dir is not None and old_dir.is_dir():
        os.rename(old_dir, new_dir)
    # 目录内 JSON 引用同步：.eval_cache.json 的 source 字段存的就是绝对路径
    if new_dir is not None and new_dir.is_dir():
        for jf in new_dir.rglob("*.json"):
            try:
                content = jf.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if plan["old_id"] in content:
                jf.write_text(
                    content.replace(plan["old_id"], plan["new_id"]), encoding="utf-8"
                )
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE qm_user_models
                SET model_id = :new, storage_path = :new_storage,
                    updated_at = :now
                WHERE tenant_id = :t AND user_id = :u AND model_id = :old
                """
            ),
            {
                "new": plan["new_id"],
                "new_storage": plan["new_storage"],
                "now": datetime.now(timezone.utc),
                "t": plan["tenant_id"],
                "u": plan["user_id"],
                "old": plan["old_id"],
            },
        )
    # 写后回读复验（落库的那份 + 磁盘那份）
    with engine.connect() as conn:
        got = conn.execute(
            text(
                """
                SELECT model_id, storage_path FROM qm_user_models
                WHERE tenant_id = :t AND user_id = :u AND model_id = :new
                """
            ),
            {"t": plan["tenant_id"], "u": plan["user_id"], "new": plan["new_id"]},
        ).first()
    assert got is not None, f"回读失败: {plan['new_id']} 未落库"
    assert got[0] == plan["new_id"], f"回读 model_id 不一致: {got[0]}"
    assert got[1] == plan["new_storage"], f"回读 storage_path 不一致: {got[1]}"
    if new_dir is not None:
        assert new_dir.is_dir(), f"回读目录不存在: {new_dir}"
        leftover = [
            str(jf)
            for jf in new_dir.rglob("*.json")
            if plan["old_id"] in jf.read_text(encoding="utf-8", errors="ignore")
        ]
        assert not leftover, f"目录内仍有旧 ID 引用: {leftover}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="执行写入（默认 dry-run）")
    args = parser.parse_args()

    engine = _get_engine()
    bad_rows = _scan(engine)
    print(f"扫描 qm_user_models：非 ASCII/非法 model_id {len(bad_rows)} 条")
    if not bad_rows:
        print("无需修复（幂等空操作）。")
        return 0

    plans = [_plan_one(engine, r) for r in bad_rows]
    blockers = [p for p in plans if p["issues"]]
    for p in plans:
        flag = "跳过" if p["issues"] else "可修"
        print(f"  [{flag}] {p['old_id']}")
        print(f"        → {p['new_id']}")
        print(f"        目录: {p['old_dir'] or '(无)'} → {p['new_dir'] or '(无)'}")
        for issue in p["issues"]:
            print(f"        ! {issue}")

    if blockers:
        print(f"\n{len(blockers)} 条有阻塞项，未写入。")
        return 2

    if not args.apply:
        print("\n（dry-run，未写入。加 --apply 执行）")
        return 0

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = DEFAULT_MODELS_ROOT.parent / f".repair_backup_nonasciiids_{ts}.json"
    backup_path.write_text(
        json.dumps(plans, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"备份已写入: {backup_path}")

    for p in plans:
        _apply_one(engine, p)
        print(f"已修复: {p['old_id']} → {p['new_id']}")

    # 终验：重扫应无残留
    residue = _scan(engine)
    if residue:
        print(f"残留 {len(residue)} 条未修复: {[r['model_id'] for r in residue]}")
        return 1
    print("终验通过：无残留非 ASCII model_id。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
