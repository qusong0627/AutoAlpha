"""全局股票池 - 唯一解析入口 PoolResolver（v2：TXT 即事实源）。

所有功能（回测 / 训练 / 推理 / 模拟盘 / 实盘 / 因子挖掘 / Strategy Lab SDK）
都必须经本模块把「池引用」解析成 `PoolSnapshot`，不再各自维护白名单与路径探测。

支持的 ref 语法（兼容历史写法）：

| ref                          | 含义                                        |
|------------------------------|---------------------------------------------|
| `pool:csi300`                | 库内池（按 code），读其成员 TXT              |
| `pool_id:sp_xxx_ab12cd34`    | 按 pool_id                                  |
| `csi300` / `all_a`           | 裸池 code（历史写法）                        |
| `list:SH600036,SZ000001`     | 内联列表                                     |
| `file:/abs/x.txt`            | 本地文件（每行一个代码 / csv 带表头）        |
| `/abs/x.txt`                 | 同上（历史写法：裸路径）                     |
| `all`                        | **不过滤**（`unfiltered=True`）              |
| `cos://...`                  | 回测运行时已解析 → resolver 透传告警          |
| `user_strategies/...`        | 旧自定义池路径 → 同上                        |

内置池（is_system）如果 TXT 缺失/为空，会即时从 QuantDB 指数权重拉取并
**回写 TXT**（自愈），之后所有消费者直接读文件。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable, Sequence

from sqlalchemy import text as sql_text

from .constants import SCOPE_GLOBAL
from .materializer import (
    pool_txt_path,
    read_pool_txt,
    resolve_pool_txt,
    write_pool_txt,
)
from .normalize import (
    checksum_symbols,
    normalize_market,
    normalize_symbols,
    to_api_symbol,
)
from .schemas import PoolSnapshot

logger = logging.getLogger(__name__)

# 成分提供方签名：(market, index_symbol, pool_code) -> [api_symbol,...]
IndexProvider = Callable[[str, "str | None", str], list[str]]

_index_provider: IndexProvider | None = None


def register_index_provider(provider: IndexProvider | None) -> None:
    """注册指数成分提供方（engine 侧启动时注入 QuantDB 实现）。"""
    global _index_provider
    _index_provider = provider


def _default_index_provider(
    market: str, index_symbol: str | None, pool_code: str = ""
) -> list[str]:
    """默认实现：惰性走 QuantDB 指数权重（函数内导入，避免 shared 依赖 engine）。"""
    mk = normalize_market(market)
    if mk == "CN":
        if not pool_code:
            return []
    elif not index_symbol:
        return []

    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    hub = QuantDBDataHub.get_instance()

    if mk == "CN":
        # QuantDB 的口径是「池名」（csi300 / all_a / ...），不是指数代码
        df = hub.fetch_universe_stocks(pool_code)
    else:
        # 非 CN：指数权重（QuantHK/QuantUS 接入前返回空，调用方给显式告警）
        df = None

    if df is None or getattr(df, "empty", True):
        return []
    if "symbol" not in df.columns:
        return []
    out: list[str] = []
    for row in df.to_dict("records"):
        sym = str(row.get("symbol") or "").strip()
        if sym:
            out.append(sym)
    return out


@dataclass
class ResolveContext:
    """解析上下文：决定可见范围与代码口径。"""

    tenant_id: str | None = None
    user_id: str | None = None
    market: str | None = None  # 显式市场覆盖（优先于池自身 market）

    def normalized_market(self, fallback: str = "CN") -> str:
        return normalize_market(self.market or fallback)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
class PoolResolver:
    async def resolve(
        self,
        ref: str | None,
        ctx: ResolveContext | None = None,
        *,
        strict: bool = False,
    ) -> PoolSnapshot:
        return await asyncio.to_thread(self.resolve_sync, ref, ctx, strict=strict)

    def resolve_sync(
        self,
        ref: str | None,
        ctx: ResolveContext | None = None,
        *,
        strict: bool = False,
    ) -> PoolSnapshot:
        ctx = ctx or ResolveContext()
        raw = (ref or "").strip()

        try:
            return self._dispatch(raw, ctx)
        except Exception as exc:  # noqa: BLE001
            if strict:
                raise
            logger.warning("股票池解析失败 ref=%r: %s", raw, exc)
            return PoolSnapshot(
                pool_id=f"unresolved:{raw}",
                code=raw,
                market=ctx.normalized_market(),
                source="unresolved",
                warnings=[f"解析失败: {exc}"],
            )

    # ------------------------------------------------------------------
    # 分派
    # ------------------------------------------------------------------
    def _dispatch(self, raw: str, ctx: ResolveContext) -> PoolSnapshot:
        if not raw:
            return self._unfiltered("", ctx, "ref 为空 → 视为不过滤")

        lowered = raw.lower()

        if lowered in ("all", "all_a_share", "*"):
            return self._unfiltered(raw, ctx, "ref=all → 不过滤（全市场）")

        if lowered.startswith("pool_id:"):
            return self._from_pool_id(raw.split(":", 1)[1].strip(), ctx)

        if lowered.startswith("pool:"):
            return self._from_code(raw.split(":", 1)[1].strip(), ctx)

        if lowered.startswith("list:"):
            return self._from_inline(raw.split(":", 1)[1], ctx)

        if lowered.startswith("file:"):
            return self._from_file(raw.split(":", 1)[1].strip(), ctx)

        if lowered.startswith("cos://"):
            return self._unsupported(
                raw,
                ctx,
                "cos:// 引用由回测运行时解析（ai_strategy cos_uploader）；"
                "新链路请先导入为池（POST /api/v1/admin/stock-pools/create-from-members）",
            )

        if lowered.startswith("user_pool:") or "user_strategies/" in raw:
            return self._unsupported(
                raw,
                ctx,
                "旧自定义池路径由回测运行时解析（stock_pool_files）；"
                "新链路请迁移为池记录",
            )

        # 裸路径（历史写法：universe 直接是本地 txt 路径）
        if "/" in raw or raw.endswith(".txt") or raw.endswith(".csv"):
            return self._from_file(raw, ctx)

        # 裸 code：先查库，再回内置
        return self._from_code(raw, ctx)

    # ------------------------------------------------------------------
    # 各来源
    # ------------------------------------------------------------------
    def _from_pool_id(self, pool_id: str, ctx: ResolveContext) -> PoolSnapshot:
        from backend.shared.database_pool import get_db

        with get_db() as session:
            row = (
                session.execute(
                    sql_text("SELECT * FROM qm_stock_pool WHERE pool_id = :pid"),
                    {"pid": pool_id},
                )
                .mappings()
                .first()
            )
            if row is None:
                return PoolSnapshot(
                    pool_id=pool_id,
                    code=pool_id,
                    market=ctx.normalized_market(),
                    source="missing",
                    warnings=[f"pool_id 不存在: {pool_id}"],
                )
            return self._snapshot_for_row(session, dict(row), ctx)

    def _from_code(self, code: str, ctx: ResolveContext) -> PoolSnapshot:
        code = (code or "").strip()
        if not code:
            return self._unfiltered("", ctx, "code 为空 → 不过滤")

        from backend.shared.database_pool import get_db

        with get_db() as session:
            row = self._query_pool_by_code(session, code, ctx)
            if row is None:
                builtin = self._builtin_or_none(code)
                if builtin is not None:
                    return self._from_builtin(builtin, ctx)
                return PoolSnapshot(
                    pool_id=f"missing:{code}",
                    code=code,
                    market=ctx.normalized_market(),
                    source="missing",
                    warnings=[f"未知股票池 code={code}（库内无记录，内置目录也没有）"],
                )
            return self._snapshot_for_row(session, dict(row), ctx)

    @staticmethod
    def _builtin_or_none(code: str):
        from .builtins import get_builtin

        return get_builtin(code)

    def _from_builtin(self, builtin, ctx: ResolveContext) -> PoolSnapshot:
        """库内没有记录（fresh install / seed 未跑）：直接按内置目录取并自愈写 TXT。"""
        market = ctx.normalized_market(builtin.market)
        symbols = _fetch_builtin_symbols(builtin)
        warnings: list[str] = []
        if not symbols:
            warnings.append(
                f"内置池 {builtin.code} 暂无成分（QuantDB 未就绪或数据源未接入）"
            )
        api = [to_api_symbol(s, market) for s in symbols]
        try:
            write_pool_txt(
                pool_txt_path("global", builtin.code),
                api,
                header=f"builtin {builtin.code} ({builtin.name})",
            )
        except OSError as exc:
            logger.warning("内置池 TXT 自愈写入失败 %s: %s", builtin.code, exc)
        return _build_snapshot(
            pool_id=f"sys_{builtin.code}",
            code=builtin.code,
            market=market,
            api_symbols=api,
            source="builtin",
            warnings=warnings,
        )

    def _from_inline(self, body: str, ctx: ResolveContext) -> PoolSnapshot:
        market = ctx.normalized_market()
        symbols = normalize_symbols(
            [s for s in body.replace(";", ",").split(",") if s.strip()], market
        )
        return _build_snapshot(
            pool_id="inline",
            code="inline",
            market=market,
            storage_symbols=symbols,
            source="inline",
            warnings=[],
        )

    def _from_file(self, path: str, ctx: ResolveContext) -> PoolSnapshot:
        market = ctx.normalized_market()
        fp = Path(path)
        if not fp.is_absolute():
            fp = Path.cwd() / fp
        if not fp.exists():
            return PoolSnapshot(
                pool_id=f"file:{path}",
                code=path,
                market=market,
                source="file",
                warnings=[f"股票池文件不存在: {fp}"],
            )

        text = fp.read_text(encoding="utf-8", errors="ignore")
        raw_symbols: list[str] = []
        if fp.suffix.lower() == ".csv":
            reader = csv.reader(io.StringIO(text))
            rows = [r for r in reader if r and any(c.strip() for c in r)]
            if rows:
                header = [c.strip().lower() for c in rows[0]]
                idx = 0
                for candidate in ("symbol", "code", "证券代码", "代码"):
                    if candidate in header:
                        idx = header.index(candidate)
                        break
                else:
                    # 无表头 → 第一行当数据
                    raw_symbols.append(rows[0][idx])
                for r in rows[1:]:
                    if idx < len(r):
                        raw_symbols.append(r[idx])
        else:
            for line in text.splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                raw_symbols.append(s.split(",")[0].split("\t")[0].strip())

        symbols = normalize_symbols(raw_symbols, market)
        return _build_snapshot(
            pool_id=f"file:{fp}",
            code=fp.stem,
            market=market,
            storage_symbols=symbols,
            source="file",
            warnings=[],
        )

    # ------------------------------------------------------------------
    # 库内池（TXT 成员）
    # ------------------------------------------------------------------
    def _query_pool_by_code(self, session, code: str, ctx: ResolveContext):
        scopes = [SCOPE_GLOBAL]
        if ctx.tenant_id:
            scopes.append("tenant")
        if ctx.user_id:
            scopes.append("user")

        for scope in scopes:
            row = (
                session.execute(
                    sql_text(
                        """
                    SELECT * FROM qm_stock_pool
                     WHERE code = :code
                       AND scope = :scope
                       AND (
                            scope = 'global'
                         OR tenant_id = :tid
                         OR owner_user_id = :uid
                       )
                       AND status <> 'archived'
                     ORDER BY CASE scope WHEN 'user' THEN 0 WHEN 'tenant' THEN 1 ELSE 2 END
                     LIMIT 1
                    """
                    ),
                    {
                        "code": code,
                        "scope": scope,
                        "tid": ctx.tenant_id,
                        "uid": ctx.user_id,
                    },
                )
                .mappings()
                .first()
            )
            if row is not None:
                return row
        return None

    def _snapshot_for_row(self, session, data: dict, ctx: ResolveContext) -> PoolSnapshot:
        pool_id = str(data["pool_id"])
        code = str(data["code"])
        market = ctx.normalized_market(data.get("market") or "CN")
        scope = str(data.get("scope") or SCOPE_GLOBAL)
        is_system = bool(data.get("is_system"))
        warnings: list[str] = []

        file_path = resolve_pool_txt(
            scope,
            code,
            file_path=data.get("file_path"),
            tenant_id=data.get("tenant_id"),
            owner_user_id=data.get("owner_user_id"),
        )
        api = read_pool_txt(file_path)

        if not api and is_system:
            # 内置池 TXT 缺失/为空 → 从 QuantDB 拉取并回写（自愈）
            builtin = self._builtin_or_none(code)
            fetched = _fetch_builtin_symbols(builtin) if builtin else []
            if fetched:
                api = [to_api_symbol(s, market) for s in fetched]
                try:
                    write_pool_txt(file_path, api, header=f"builtin {code}")
                    _update_pool_counts_sync(session, pool_id, api)
                except OSError as exc:
                    logger.warning("内置池 TXT 自愈写入失败 %s: %s", code, exc)
                warnings.append(f"内置池 {code} 成员 TXT 缺失，已从 QuantDB 刷新")
            else:
                warnings.append(
                    f"内置池 {code} 暂无成分（QuantDB 未就绪或数据源未接入），"
                    "TXT 为空；成分同步后自动恢复"
                )
        elif not api:
            warnings.append(f"股票池 {code} 成员为空（文件: {file_path}）")

        return _build_snapshot(
            pool_id=pool_id,
            code=code,
            market=market,
            api_symbols=api,
            source="pool",
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    def _unfiltered(self, raw: str, ctx: ResolveContext, reason: str) -> PoolSnapshot:
        return PoolSnapshot(
            pool_id="all",
            code="all",
            market=ctx.normalized_market(),
            source="all",
            unfiltered=True,
            warnings=[reason],
        )

    def _unsupported(self, raw: str, ctx: ResolveContext, reason: str) -> PoolSnapshot:
        return PoolSnapshot(
            pool_id=f"unsupported:{raw}",
            code=raw,
            market=ctx.normalized_market(),
            source="unsupported",
            warnings=[reason],
        )


resolver = PoolResolver()


# ---------------------------------------------------------------------------
# 组装 / 工具
# ---------------------------------------------------------------------------
def _build_snapshot(
    *,
    pool_id: str,
    code: str,
    market: str,
    warnings: list[str],
    source: str,
    api_symbols: Sequence[str] | None = None,
    storage_symbols: Sequence[str] | None = None,
) -> PoolSnapshot:
    if api_symbols is None:
        api_list = [to_api_symbol(s, market) for s in storage_symbols or []]
        sym_list = list(storage_symbols or [])
    else:
        api_list = [str(s) for s in api_symbols]
        sym_list = normalize_symbols(api_list, market)
    return PoolSnapshot(
        pool_id=pool_id,
        code=code,
        market=market,
        checksum=checksum_symbols(sym_list) if sym_list else None,
        symbols=sym_list,
        api_symbols=api_list,
        source=source,
        warnings=list(warnings),
    )


def _fetch_builtin_symbols(builtin) -> list[str]:
    """内置池成分（任意口径输入 → 后缀式去重输出）。"""
    if builtin is None:
        return []
    provider = _index_provider or _default_index_provider
    try:
        raw = provider(builtin.market, builtin.index_symbol, builtin.code)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "内置池成分读取失败 market=%s code=%s: %s", builtin.market, builtin.code, exc
        )
        return []
    return normalize_symbols(raw or [], builtin.market)


def _update_pool_counts_sync(session, pool_id: str, api_symbols: Sequence[str]) -> None:
    """自愈刷新后同步 DB 计数/校验和。checksum 基于 API 口径集合（稳定）。"""
    session.execute(
        sql_text(
            """
            UPDATE qm_stock_pool
               SET symbol_count = :count,
                   checksum = :checksum,
                   updated_at = NOW()
             WHERE pool_id = :pid
            """
        ),
        {
            "pid": pool_id,
            "count": len(api_symbols),
            "checksum": checksum_symbols(sorted(api_symbols)),
        },
    )
    session.commit()


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
async def resolve_pool(
    ref: str | None,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    market: str | None = None,
    strict: bool = False,
) -> PoolSnapshot:
    return await resolver.resolve(
        ref,
        ResolveContext(tenant_id=tenant_id, user_id=user_id, market=market),
        strict=strict,
    )


def resolve_pool_sync(
    ref: str | None,
    *,
    tenant_id: str | None = None,
    user_id: str | None = None,
    market: str | None = None,
    strict: bool = False,
) -> PoolSnapshot:
    return resolver.resolve_sync(
        ref,
        ResolveContext(tenant_id=tenant_id, user_id=user_id, market=market),
        strict=strict,
    )
