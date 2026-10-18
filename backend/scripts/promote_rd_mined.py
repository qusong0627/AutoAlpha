#!/usr/bin/env python3
"""挖掘因子毕业器：``quantcustom`` → ``quantdb`` 的 ``rd_mined`` 镜像。

**为什么需要它。** 挖掘产物按设计落 ``$QM_QUANTCUSTOM_DATA_DIR``（CUSTOM
市场，见 ``prompts/rd-agent-factor-mining.md``「勿写 quantdb」），而因子研究页
与 CN 训练都只读 ``$QM_QUANTDB_DATA_DIR``。两条链因此互不可见——挖掘出来的
因子能在 CUSTOM 侧训练，却永远进不了 CN 的因子研究与 CN 模型。本脚本就是那道
**显式**的闸门：由人决定「这一批毕业」，而不是让挖掘流水线偷偷写官方库。

**语义边界（都是刻意的）：**

1. **镜像，不是合并**。目标根下独立成库 ``quantdb/6_ml_datasets/rd_mined``，
   与 ``l1_factors`` / ``l2_factors`` 并列；绝不把列并进 l1/l2。既有先例是
   ``gap_mined``（同样是 quantdb 根下的独立目录）。
2. **不注入 OHLCV**。``rd_mined`` 是纯因子列 + ``symbol``/``date``；CN 读取层
   对「只缺 OHLCV」的源有 donor 机制（``_donor_has_ohlcv`` 查同市场根下的
   ``l1_factors``），ccass / south 走的就是这条路。补 OHLCV 反而会让它从
   「次要源」变成「另一份行情」，制造第二真相源。
3. **增量按 (size, mtime_ns)**。物化器新增因子时会**重写所有分区**对齐列集
   （训练读取层无 union_by_name，列漂移会响亮失败），所以列集一变，全部
   分区的时间戳都会变 —— 增量天然捕获，不需要额外的 schema 比对。
4. **原子落盘**。先写 ``.tmp`` 再 ``os.replace``，避免训练进程读到半截 parquet。
5. **默认不删**。源里没有的分区留在目标里（保守：误删会让训练静默少一段历史）；
   要收敛用 ``--prune``，且它只删「目标有、源没有」的 ``dt=*`` 分区。
6. **幂等**。重复跑不产生任何写入。

典型用法::

    python backend/scripts/promote_rd_mined.py --dry-run              # 先看会拷什么
    python backend/scripts/promote_rd_mined.py                        # 增量镜像
    python backend/scripts/promote_rd_mined.py --register             # 镜像 + 刷新 CN 字段
    python backend/scripts/promote_rd_mined.py --register --publish   # 再发布 CN 训练目录

毕业后在「训练数据集」页刷新字段 → 发布 CN 训练目录；重建因子研究快照后，
这批因子会以独立来源库出现在研究页（``build_factor_panel_private.py`` 默认
``auto`` 扫描 ``6_ml_datasets``，无需改代码）。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logger = logging.getLogger("promote_rd_mined")

DATASET = "rd_mined"
MANIFEST_NAME = "_promote_manifest.json"
TARGET_MARKET = "CN"

# 源根：CUSTOM 市场的数据根；目标根：CN。缺省与 quantdb_factor_reader 的
# MARKET_DATA_ROOT 保持一致（env 优先，其次容器内约定路径）。
_SOURCE_ENV = "QM_QUANTCUSTOM_DATA_DIR"
_TARGET_ENV = "QM_QUANTDB_DATA_DIR"
_SOURCE_DEFAULT = "/data/quantcustom"
_TARGET_DEFAULT = "/data/quantdb"

# 分区名形如 dt=20200102；只认这个形状，其它文件（清单等）一律不碰。
_PARTITION_PREFIX = "dt="
_PARTITION_FILE = "data.parquet"


@dataclass(frozen=True)
class Partition:
    """一个 ``dt=*`` 分区的指纹。"""

    name: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class SyncPlan:
    """一次镜像要做的全部动作（纯计算结果，可离线断言）。"""

    to_copy: tuple[str, ...]
    to_prune: tuple[str, ...]
    unchanged: tuple[str, ...]

    @property
    def is_noop(self) -> bool:
        return not self.to_copy and not self.to_prune

    def summary(self) -> str:
        return (
            f"待拷贝 {len(self.to_copy)} / 待删除 {len(self.to_prune)} / "
            f"未变化 {len(self.unchanged)}"
        )


@dataclass
class PromoteResult:
    copied: list[str] = field(default_factory=list)
    pruned: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)


def resolve_roots(
    source_root: str | Path | None = None,
    target_root: str | Path | None = None,
) -> tuple[Path, Path]:
    """解析源/目标数据根。显式入参 > 环境变量 > 容器内约定路径。"""
    src = Path(source_root or os.environ.get(_SOURCE_ENV) or _SOURCE_DEFAULT)
    dst = Path(target_root or os.environ.get(_TARGET_ENV) or _TARGET_DEFAULT)
    return src / "6_ml_datasets" / DATASET, dst / "6_ml_datasets" / DATASET


def scan_partitions(dataset_root: Path) -> dict[str, Partition]:
    """扫描 ``dt=*/data.parquet``；目录不存在返回空（首次毕业即正常情况）。"""
    if not dataset_root.is_dir():
        return {}
    out: dict[str, Partition] = {}
    for path in dataset_root.glob(f"{_PARTITION_PREFIX}*/{_PARTITION_FILE}"):
        try:
            st = path.stat()
        except OSError as exc:  # 并发写盘时可能瞬时消失
            logger.warning("跳过不可读分区 %s: %s", path, exc)
            continue
        out[path.parent.name] = Partition(path.parent.name, st.st_size, st.st_mtime_ns)
    return out


def plan_sync(
    source: Mapping[str, Partition],
    target: Mapping[str, Partition],
    *,
    force: bool = False,
    prune: bool = False,
) -> SyncPlan:
    """算出「源 → 目标」该拷哪些、该删哪些。

    判据是 ``(size, mtime_ns)`` 全等即视为一致。**不**逐字节比对：分区是
    3~4MB 量级的 parquet，全量哈希会让一次毕业从分钟级变成十分钟级，而物化器
    重写分区必然是「新文件 + 新时间戳」，指纹已足够。
    """
    to_copy: list[str] = []
    unchanged: list[str] = []
    for name in sorted(source):
        src, dst = source[name], target.get(name)
        if force or dst is None or (dst.size, dst.mtime_ns) != (src.size, src.mtime_ns):
            to_copy.append(name)
        else:
            unchanged.append(name)
    to_prune = sorted(set(target) - set(source)) if prune else []
    return SyncPlan(tuple(to_copy), tuple(to_prune), tuple(unchanged))


def _copy_partition(src_file: Path, dst_file: Path) -> None:
    """原子拷贝：同目录 ``.tmp`` → ``os.replace``，读者永远看到完整文件。"""
    dst_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_file.with_suffix(dst_file.suffix + ".tmp")
    try:
        shutil.copy2(src_file, tmp)
        os.replace(tmp, dst_file)  # 同文件系统内原子
    finally:
        tmp.unlink(missing_ok=True)


def apply_sync(
    source_root: Path,
    target_root: Path,
    plan: SyncPlan,
    *,
    dry_run: bool = False,
) -> PromoteResult:
    """执行镜像。单个分区失败不中断整批——记进 ``failed`` 由调用方处置。"""
    result = PromoteResult()
    if dry_run:
        result.copied = list(plan.to_copy)
        result.pruned = list(plan.to_prune)
        return result

    for name in plan.to_copy:
        src_file = source_root / name / _PARTITION_FILE
        dst_file = target_root / name / _PARTITION_FILE
        try:
            _copy_partition(src_file, dst_file)
            result.copied.append(name)
        except OSError as exc:
            logger.error("拷贝失败 %s: %s", name, exc)
            result.failed.append({"partition": name, "error": str(exc)})

    for name in plan.to_prune:
        try:
            shutil.rmtree(target_root / name)
            result.pruned.append(name)
        except OSError as exc:
            logger.error("删除失败 %s: %s", name, exc)
            result.failed.append({"partition": name, "error": str(exc)})
    return result


def _load_manifest(dataset_root: Path) -> dict[str, Any]:
    path = dataset_root / MANIFEST_NAME
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("清单不可读，按空清单处理: %s", exc)
        return {}


def _save_manifest(dataset_root: Path, data: Mapping[str, Any]) -> None:
    dataset_root.mkdir(parents=True, exist_ok=True)
    path = dataset_root / MANIFEST_NAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 CUSTOM 的挖掘因子库 rd_mined 镜像到 quantdb（CN）",
    )
    parser.add_argument("--source-root", help=f"源数据根（默认 ${_SOURCE_ENV}）")
    parser.add_argument("--target-root", help=f"目标数据根（默认 ${_TARGET_ENV}）")
    parser.add_argument("--force", action="store_true", help="忽略指纹，全量重拷")
    parser.add_argument(
        "--prune",
        action="store_true",
        help="删除「目标有、源没有」的分区（默认保留，避免误删历史）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不落盘")
    parser.add_argument(
        "--register",
        action="store_true",
        help="镜像后刷新 CN 字段注册（与「刷新字段」按钮同一路径）",
    )
    parser.add_argument(
        "--publish",
        action="store_true",
        help="注册后发布 CN 训练目录版本（决定这批因子是否进入 CN 训练口径）",
    )
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    source_root, target_root = resolve_roots(args.source_root, args.target_root)
    logger.info("源: %s", source_root)
    logger.info("目标: %s", target_root)
    if not source_root.is_dir():
        logger.error("源库不存在，无可毕业内容: %s", source_root)
        return 2

    source = scan_partitions(source_root)
    target = scan_partitions(target_root)
    if not source:
        logger.error("源库下没有 dt=*/data.parquet 分区: %s", source_root)
        return 2

    plan = plan_sync(source, target, force=args.force, prune=args.prune)
    logger.info(
        "%s（源 %d 分区 / 目标 %d 分区）", plan.summary(), len(source), len(target)
    )

    result = PromoteResult()
    if plan.is_noop:
        logger.info("已是最新，无写入。")
    else:
        result = apply_sync(source_root, target_root, plan, dry_run=args.dry_run)
        if args.dry_run:
            logger.info(
                "[dry-run] 将拷贝 %d 个、删除 %d 个",
                len(result.copied),
                len(result.pruned),
            )
        else:
            logger.info(
                "已拷贝 %d 个、删除 %d 个、失败 %d 个",
                len(result.copied),
                len(result.pruned),
                len(result.failed),
            )
        if result.failed:
            for item in result.failed[:10]:
                logger.error("  %s: %s", item["partition"], item["error"])
            return 1

    if args.dry_run:
        logger.info("[dry-run] 不写清单、不注册。")
        return 0

    manifest = _load_manifest(target_root)
    entries = manifest.setdefault("partitions", {})
    for name in result.copied:
        entries[name] = {"size": source[name].size, "mtime_ns": source[name].mtime_ns}
    manifest["dataset"] = DATASET
    manifest["market"] = TARGET_MARKET
    manifest["source_root"] = str(source_root)
    manifest["last_sync"] = {
        "copied": len(result.copied),
        "pruned": len(result.pruned),
        "unchanged": len(plan.unchanged),
    }
    _save_manifest(target_root, manifest)

    if args.register:
        rc = _register_fields()
        if rc != 0:
            return rc

    if args.publish:
        # 被顶替清单按 CLI 解析出的实际源根读（评审 M5）——不能内部再
        # resolve_roots() 取默认，--source-root 指到别处时会读错清单
        rc = _publish_catalog(source_root)
        if rc != 0:
            return rc

    if not args.publish:
        logger.info(
            "下一步：训练数据集页「刷新字段」(CN) → 发布 CN 训练目录（或本脚本加 --register --publish）。"
            "因子研究页需重建快照后可见（build_factor_panel_private.py 自动收录 rd_mined，无需改代码）。"
        )
    return 0


def _register_fields() -> int:
    """刷新 CN 字段注册。

    与 ``POST /admin/training-data/sources/refresh`` 走完全相同的两个调用
    （``QuantDBFactorReader.discover`` + ``record_source_fields``），只是不带
    HTTP 依赖；刻意不复制那段 SQL，避免两处口径漂移。
    """
    import asyncio

    async def _run() -> int:
        from backend.services.api.routers.admin.quantdb_factor_catalog import (
            _ensure_schema,
            record_source_fields,
        )
        from backend.shared.database_manager_v2 import get_session
        from backend.services.engine.data_platform.quantdb_factor_reader import (
            QuantDBFactorReader,
        )

        discovered = await asyncio.to_thread(
            QuantDBFactorReader(market=TARGET_MARKET).discover, TARGET_MARKET
        )
        status = discovered.get(DATASET)
        if status is None:
            logger.error("CN 侧未发现 %s —— 目标根下没有该库或其分区不可读", DATASET)
            return 1
        # discover() 按契约返回 {source: to_dict()}（dict[str, dict]），与下方
        # record_source_fields 的入参同形——一律按字典取值，勿用属性访问。
        logger.info(
            "CN/%s: files=%s cols=%s ready=%s reason=%s",
            DATASET,
            status["files"],
            len(status["columns"]),
            status["ready"],
            status["reason"],
        )
        if not status["ready"]:
            logger.error("CN 侧未就绪，仍写注册表（便于前端显示原因），但不要发布")
        async with get_session() as session:
            await _ensure_schema(session)
            await record_source_fields(session, DATASET, status, TARGET_MARKET)
        logger.info("CN 字段注册已刷新（%s）。", DATASET)
        return 0 if status["ready"] else 1

    return asyncio.run(_run())


def _publish_catalog(source_dataset_root: Path | None = None) -> int:
    """确保 CN 的 rd_mined 有一份**已发布**目录版本。

    发布是显式动作（``--publish``）：它决定这批因子是否真的进入 CN 训练口径。
    已有草稿则发布该草稿；没有草稿则新建 + 全量播种 + 发布。

    **被顶替列（T-FM-02 替换机制）**：superseded 列的值随镜像原样进 CN（写值
    不删），但目录面必须与 CUSTOM 侧的活跃口径一致——播种 / 既有草稿 / 已发布
    修正三条路径都显式禁用它们。已发布版本不可改写：线上那份若仍启用被顶替列，
    不静默放过，从线上克隆一份新草稿（保留手工勾选现状）、禁用后重新发布。被
    顶替清单从**源侧**（CUSTOM）materialize manifest 读，那是替换机制的落账处；
    ``source_dataset_root`` 缺省 = ``resolve_roots()`` 的默认源库，``main()``
    会把 CLI 解析出的实际源根传进来——``--source-root`` 指到别处时被顶替清单
    必须跟着走，否则会对默认库读清单、给指定库发版，两处口径劈叉（评审 M5）。

    并发约定：本脚本按**单跑**使用（与 HTTP 发布端点并发时可产生同源双
    published，消费方按 published_at 取最新一份；靠人工纪律而非 DB 锁，
    T-FM-02 评审 M7 记录的残余风险）。
    """
    import asyncio

    async def _run() -> int:
        from sqlalchemy import text

        from backend.scripts.rd_mined_materialize import (
            _load_manifest as _load_materialize_manifest,
            _superseded_columns,
        )
        from backend.services.api.routers.admin.quantdb_factor_catalog import (
            _ensure_schema,
            create_catalog_draft,
            disable_catalog_mappings,
            publish_catalog_version,
            seed_catalog_mappings,
            seed_draft_from_published,
        )
        from backend.shared.database_manager_v2 import get_session

        if source_dataset_root is None:
            source_root, _target_root = resolve_roots()
        else:
            source_root = Path(source_dataset_root)
        superseded = _superseded_columns(_load_materialize_manifest(source_root))
        if superseded:
            logger.info(
                "CUSTOM 侧有 %d 个被顶替（superseded）列，目录发布将同步禁用",
                len(superseded),
            )

        async def _enabled_columns(session, version_id: str) -> set[str]:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT source_column FROM qm_training_factor_mapping "
                            "WHERE version_id = :vid AND enabled"
                        ),
                        {"vid": version_id},
                    )
                )
                .scalars()
                .all()
            )
            return {str(c) for c in rows}

        async def _disable_and_verify(version_id: str) -> int | None:
            """禁用被顶替列并复核确已生效；有残留 → 回滚并返回 None（评审 M3）。

            发布不可逆（旧版本转 archived），禁用行数只写日志不够：UPDATE
            因口径漂移等原因命中不全时，必须中止，而不是带着仍启用的已下架
            列发布。复核用与「已是最新」判据同一条查询（口径单源）。
            """
            if not superseded:
                return 0
            disabled = await disable_catalog_mappings(
                session, version_id, sorted(superseded)
            )
            still = superseded & await _enabled_columns(session, version_id)
            if still:
                logger.error(
                    "版本 %s 禁用后仍启用被顶替列 %s：回滚并中止发布",
                    version_id,
                    sorted(still),
                )
                await session.rollback()
                return None
            return disabled

        async with get_session() as session:
            await _ensure_schema(session)
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT version_id, status FROM qm_training_factor_catalog_version "
                            "WHERE market = :m AND source_dataset = :d "
                            "ORDER BY (status = 'draft') DESC, created_at DESC LIMIT 1"
                        ),
                        {"m": TARGET_MARKET, "d": DATASET},
                    )
                )
                .mappings()
                .first()
            )
            if row and str(row["status"]) == "published":
                version_id = str(row["version_id"])
                stale = superseded & await _enabled_columns(session, version_id)
                if not stale:
                    logger.info(
                        "CN/%s 已有已发布目录版本 %s，无需发布。",
                        DATASET,
                        version_id,
                    )
                    return 0
                logger.info(
                    "已发布版本 %s 仍启用 %d 个被顶替列：克隆线上 → 禁用 → 重新发布",
                    version_id,
                    len(stale),
                )
                new_id = await create_catalog_draft(
                    session,
                    DATASET,
                    f"{DATASET}（顶替下架修正）",
                    TARGET_MARKET,
                    created_by="promote_rd_mined",
                )
                cloned = await seed_draft_from_published(
                    session,
                    draft_id=new_id,
                    market=TARGET_MARKET,
                    source_dataset=DATASET,
                )
                if cloned == 0:
                    # 线上明明启用着被顶替列却一行没抄到：发布会把旧版转
                    # archived 且不可逆，宁可中止也不发布空版本。回滚丢掉
                    # 这枚空草稿——不 rollback 的话 get_session 会把「正常
                    # return」当成功提交，孤儿草稿被后续 promote 选中，
                    # publish 又因 0 行失败，目录永久卡死（评审 M4）。
                    logger.error(
                        "克隆线上版本 %s 得到 0 行——中止发布，请人工检查目录版本表",
                        version_id,
                    )
                    await session.rollback()
                    return 1
                disabled = await _disable_and_verify(new_id)
                if disabled is None:
                    return 1
                await publish_catalog_version(session, new_id)
                logger.info(
                    "CN/%s 目录已重发布 %s（克隆 %d 行，禁用被顶替列 %d 个）",
                    DATASET,
                    new_id,
                    cloned,
                    disabled,
                )
                return 0
            if row:
                version_id = str(row["version_id"])
                disabled = await _disable_and_verify(version_id)
                if disabled is None:
                    return 1
                logger.info(
                    "发布既有草稿 %s（禁用被顶替列 %d 个）", version_id, disabled
                )
            else:
                version_id = await create_catalog_draft(
                    session,
                    DATASET,
                    f"{DATASET}（挖掘毕业）",
                    TARGET_MARKET,
                    created_by="promote_rd_mined",
                )
                seeded = await seed_catalog_mappings(
                    session,
                    {
                        "version_id": version_id,
                        "source_dataset": DATASET,
                        "market": TARGET_MARKET,
                    },
                )
                disabled = await _disable_and_verify(version_id)
                if disabled is None:
                    return 1
                logger.info(
                    "新建草稿 %s，播种 %s 条映射（禁用被顶替列 %d 个）",
                    version_id,
                    seeded.get("seeded_fields"),
                    disabled,
                )
            await publish_catalog_version(session, version_id)
        logger.info("CN/%s 目录已发布。", DATASET)
        return 0

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
