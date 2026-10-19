"""因子挖掘独立后端最小入口（WSL 部署用，不改原仓库任何代码）.

设计目标
--------
把 quantmindoss@next 的因子挖掘链路从 engine 单体里摘出来单独跑：

    alpha-agent / alpha-agent docs   (挖掘任务、因子池、轨迹、文档链)
    factor-backtest                  (跨市场回测中心)
    factor-report                    (数据集快照报告)
    factor-research                  (因子研究工作台)
    admin 层                         (挖掘因子→训练特征毕业、rd_mined 物化控制台)

与原 `backend/services/engine/main.py` 的差异（刻意为之）：
1. 只挂这些路由，不挂 qlib_app / ai_strategy / quantbot / trade 等重模块；
2. 认证中间件与 CORS 保持与 engine/main.py **同口径**（同一份
   `backend.shared.auth`、同一组 `PROTECTED_PREFIXES`），避免独立部署后
   把内部精度接口暴露成匿名可达；
3. 建表 DDL 默认关闭（`ENABLE_STARTUP_DDL=1` 才执行）；任务租约对账与日志留存
   清理仍会尝试运行，但失败只告警、不阻断服务启动，缺 PG 也能查看 `/health`。

失败可见性：每个路由独立 try/except，挂载失败的进 `/health.failed`，
不让一个模块的依赖问题拖垮整个进程。
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.shared.secret_redaction import install_secret_redaction, redact_data
from backend.shared.request_id import install_request_id_middleware
from backend.shared.request_logging import install_access_log_middleware
from backend.shared.service_health_metrics import (
    set_service_health,
)

logging.basicConfig(level=logging.INFO)
install_secret_redaction()
logger = logging.getLogger("factor-mining-standalone")

_MOUNTED: list[str] = []
_FAILED: dict[str, str] = {}
_STARTUP: dict[str, str] = {}


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# ── 启动副作用（对齐 engine/main.py 的建表/对账顺序，全部非致命）────────
async def _startup_ddl() -> None:
    """建表 + 孤儿对账 + 池表。

    与 engine/main.py 的次序一致：先落库基表（rd_agent_factors / 回测台账），
    再建挖掘任务中心并 reconcile 孤儿，最后才是池三表（池是增益层）。
    """
    steps: list[tuple[str, object]] = []

    try:
        from backend.services.engine.qlib_app.services.rd_agent_persistence import (
            RDAgentFactorPersistence,
        )

        await RDAgentFactorPersistence().ensure_tables()
        steps.append(("rd_agent_factors", "ok"))
    except Exception as e:  # noqa: BLE001
        steps.append(("rd_agent_factors", f"{type(e).__name__}: {e}"))

    try:
        from backend.services.engine.factor_backtest import store as fb_store

        await fb_store.ensure_tables()
        steps.append(("factor_backtest_tables", "ok"))
    except Exception as e:  # noqa: BLE001
        steps.append(("factor_backtest_tables", f"{type(e).__name__}: {e}"))

    try:
        from backend.services.engine.alpha_agent.task_store import (
            get_mining_task_store,
        )

        store = get_mining_task_store()
        await store.ensure_tables()
        steps.append(("mining_tasks", "ok (lease sweep follows)"))
    except Exception as e:  # noqa: BLE001
        steps.append(("mining_tasks", f"{type(e).__name__}: {e}"))

    try:
        from backend.shared.factor_pool_contract import ensure_factor_pool_tables_async

        await ensure_factor_pool_tables_async()
        steps.append(("factor_pool_tables", "ok"))
    except Exception as e:  # noqa: BLE001
        steps.append(("factor_pool_tables", f"{type(e).__name__}: {e}"))

    try:
        from backend.services.engine.alpha_agent.doc_store import get_doc_store

        await get_doc_store().ensure_tables()
        steps.append(("doc_center", "ok"))
    except Exception as e:  # noqa: BLE001
        steps.append(("doc_center", f"{type(e).__name__}: {e}"))

    _STARTUP.update(dict(steps))
    for k, v in steps:
        logger.info("startup ddl %-22s %s", k, v)


async def _sweep_task_leases_once() -> None:
    """Adopt this process's expired persisted queue entries and reap stale work."""
    from backend.services.engine.alpha_agent.task_store import get_mining_task_store
    from backend.services.engine.alpha_agent.launcher import get_launcher

    store = get_mining_task_store()
    launcher = get_launcher()
    claimed = await store.claim_expired_queued_leases(launcher.queued_task_ids())
    launcher.adopt_queued_tasks(claimed)
    await store.renew_queued_leases()
    await store.reconcile_orphans()
    await launcher.drain_queue()


async def _run_task_log_gc() -> None:
    from backend.services.engine.alpha_agent.launcher import gc_task_logs

    result = await asyncio.to_thread(gc_task_logs)
    logger.info(
        "task log retention GC: pruned=%d scanned=%d",
        result["pruned"],
        result["scanned"],
    )


async def _task_lease_sweeper() -> None:
    """Periodically recover queued leases and fail tasks whose owner expired."""
    next_gc = asyncio.get_running_loop().time() + 24 * 60 * 60
    while True:
        await asyncio.sleep(30)
        try:
            await _sweep_task_leases_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("mining task lease sweep failed: %r", e)
        if asyncio.get_running_loop().time() >= next_gc:
            try:
                await _run_task_log_gc()
            except Exception as e:  # noqa: BLE001
                logger.warning("task log retention GC failed: %r", e)
            next_gc = asyncio.get_running_loop().time() + 24 * 60 * 60


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # 同步连接池（部分共享代码走 psycopg2 同步会话）；失败不阻断，只登记
    try:
        from backend.shared.database_pool import init_default_databases as init_sync

        init_sync(pool_size=10, max_overflow=5)
        _STARTUP["sync_db_pool"] = "ok"
        logger.info("sync database pool initialised")
    except Exception as e:  # noqa: BLE001
        _STARTUP["sync_db_pool"] = f"{type(e).__name__}: {e}"
        logger.warning("sync db pool init failed: %r", e)

    lease_sweeper: asyncio.Task[None] | None = None
    if _env_flag("ENABLE_STARTUP_DDL"):
        await _startup_ddl()
    else:
        logger.info("ENABLE_STARTUP_DDL off → skip table ensure")

    try:
        await _run_task_log_gc()
        _STARTUP["task_log_gc"] = "ok"
    except Exception as e:  # noqa: BLE001
        _STARTUP["task_log_gc"] = f"{type(e).__name__}: {e}"
        logger.warning("startup task log retention GC failed: %r", e)

    try:
        await _sweep_task_leases_once()
    except Exception as e:  # noqa: BLE001
        _STARTUP["task_lease_sweep"] = f"{type(e).__name__}: {e}"
        logger.warning("startup mining task lease sweep failed: %r", e)

    lease_sweeper = asyncio.create_task(
        _task_lease_sweeper(), name="mining-task-lease-sweeper"
    )
    _STARTUP["task_lease_sweeper"] = "running"

    # P0-4：预热因子推广的进程池。spawn 子进程要重新 import 整条依赖链并初始化
    # qlib（实测冷启动 >15s），把这段开销放到启动期，别让它吃掉第一次请求的超时预算。
    # 提交后不等待，失败也不致命。
    try:
        from backend.services.api.routers.admin.alpha_factor_pipeline import (
            warm_promote_executor,
        )

        warm_promote_executor()
        _STARTUP["promote_pool_warmup"] = "submitted"
    except Exception as e:  # noqa: BLE001
        _STARTUP["promote_pool_warmup"] = f"{type(e).__name__}: {e}"
        logger.warning("promote 进程池预热提交失败: %r", e)

    try:
        yield
    finally:
        if lease_sweeper is not None:
            lease_sweeper.cancel()
            try:
                await lease_sweeper
            except asyncio.CancelledError:
                pass
    logger.info("factor-mining-standalone shutdown complete")


app = FastAPI(
    title="Factor Mining Standalone",
    version="0.1.0",
    description=(
        "quantmindoss@next 因子挖掘模块独立后端 "
        "(alpha-agent / factor-backtest / factor-report / factor-research)"
    ),
    lifespan=lifespan,
)

install_request_id_middleware(app)
install_access_log_middleware(app, service_name="factor-mining-standalone")

# Serve the built workspace from the same origin when a frontend build is present.
# This keeps API/health paths identical in dev and production without requiring a
# Vite server in production. AUTOALPHA_WEB_DIST can point at an external build dir.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_DIST = Path(
    os.getenv("AUTOALPHA_WEB_DIST", str(_REPO_ROOT / "web" / "dist"))
).expanduser().resolve()
_WEB_INDEX = _WEB_DIST / "index.html"
if _WEB_INDEX.is_file() and (_WEB_DIST / "assets").is_dir():
    app.mount(
        "/assets",
        StaticFiles(directory=str(_WEB_DIST / "assets"), check_dir=True),
        name="web-assets",
    )
    logger.info("serving built workspace from %s", _WEB_DIST)
else:
    logger.info("built workspace not found at %s; API-only mode", _WEB_DIST)

_METRICS_REGISTRY = CollectorRegistry()
HTTP_REQUESTS = Counter(
    "autoalpha_http_requests_total",
    "HTTP requests handled by the AutoAlpha factor-mining service",
    ("method", "route", "status"),
    registry=_METRICS_REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "autoalpha_http_request_duration_seconds",
    "HTTP request duration in seconds",
    ("method", "route"),
    registry=_METRICS_REGISTRY,
)
SERVICE_HEALTH = Gauge(
    "autoalpha_service_health_status",
    "Factor-mining service health status (1 healthy, 0 unhealthy)",
    ("service",),
    registry=_METRICS_REGISTRY,
)
_METRIC_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"}
)


@app.exception_handler(StarletteHTTPException)
async def _redact_http_exception(
    _request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Do not reflect credentials embedded in provider or filesystem errors."""
    return JSONResponse(
        status_code=exc.status_code,
        content=redact_data({"detail": exc.detail}),
        headers=exc.headers,
    )


# ── 挂载工具 ────────────────────────────────────────────────────────────
def _mount(name: str, modpath: str, prefix: str | None = None) -> None:
    """按模块名挂载其 `router`，失败只登记不抛出。"""
    try:
        mod = __import__(modpath, fromlist=["router"])
        router = getattr(mod, "router")
        if prefix is None:
            app.include_router(router)
        else:
            app.include_router(router, prefix=prefix)
        n = len(getattr(router, "routes", []))
        _MOUNTED.append(f"{name} ({n} routes)")
        logger.info("mounted %-22s %3d routes", name, n)
    except Exception as e:  # noqa: BLE001 - 独立部署要如实看到哪些挂了
        _FAILED[name] = f"{type(e).__name__}: {e}"
        logger.warning("mount %-22s FAILED: %r", name, e)


# ── 引擎侧：因子挖掘主链路（路由自带 prefix）─────────────────────────────
_mount("alpha-agent", "backend.services.engine.routers.alpha_agent")
_mount("alpha-agent-docs", "backend.services.engine.routers.alpha_agent_docs")
_mount("rd-agent", "backend.services.engine.routers.rd_agent")
_mount("factor-backtest", "backend.services.engine.factor_backtest.router")
_mount("factor-report", "backend.services.engine.factor_report.router")
_mount("factor-research", "backend.services.engine.factor_research.router")

# ── api 侧：挖掘因子毕业到训练特征（prefix 对齐原 api 服务）─────────────
_mount(
    "admin/alpha-factors",
    "backend.services.api.routers.admin.alpha_factor_pipeline",
    prefix="/api/v1/admin/alpha-factors",
)
_mount(
    "admin/training-data/rd-mined",
    "backend.services.api.routers.admin.rd_mined_materialize",
    prefix="/api/v1/admin/training-data/rd-mined",
)
_mount(
    "admin/training-data",
    "backend.services.api.routers.admin.quantdb_factor_catalog",
    prefix="/api/v1/admin/training-data",
)


# ── 认证中间件（与 engine/main.py 同口径）───────────────────────────────
# 必须持有用户身份（不只是内部密钥）才能访问的前缀。
# 每一项必须带结尾斜杠，判定用 str.startswith，否则会连带放行同前缀的兄弟路径。
PROTECTED_PREFIXES: tuple[str, ...] = (
    "/api/v1/alpha-agent/",
    "/api/v1/rd-agent/",
    "/api/v1/admin/",
    "/api/v1/factor-research/",
)


def _dev_identity() -> dict:
    """免登录模式下的替身管理员身份。

    必须带 `is_admin=True`：api 侧 `require_admin` 先看这个字段，缺失才会去查
    RBAC 角色表——本部署没有用户库，查不到就会 403。
    """
    return {
        "user_id": os.getenv("DEV_USER_ID", "10000001"),
        "tenant_id": os.getenv("DEV_TENANT_ID", "default"),
        "username": "admin",
        "email": "admin@example.com",
        "is_admin": True,
        "roles": ["admin"],
        "jti": None,
    }


def _is_loopback_host(value: str | None) -> bool:
    """Accept only localhost names and IP loopback addresses in no-auth mode."""
    if not value or value != value.strip() or any(char in value for char in "/\\@#?"):
        return False
    try:
        authority = urlsplit(f"//{value}")
        host = authority.hostname
        port = authority.port  # accessing this also rejects malformed/out-of-range ports
    except ValueError:
        return False
    if authority.netloc.endswith(":") or (
        port is not None and not 1 <= port <= 65535
    ):
        return False
    if not host:
        return False
    host = host.rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_client(request: Request) -> bool:
    """Require a numeric loopback peer; Host headers are client-controlled."""
    client = request.client
    if client is None:
        return False
    try:
        address = ipaddress.ip_address(client.host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped.is_loopback
    return address.is_loopback


def _allowed_local_origins() -> set[str]:
    """Return explicitly configured loopback origins, never a wildcard."""
    from backend.shared.cors import DEFAULT_DEV_ORIGINS, resolve_cors_origins

    # Vite preview uses 4173 by default and shares the local API proxy.
    local_defaults = [
        *DEFAULT_DEV_ORIGINS,
        "http://127.0.0.1:4173",
        "http://localhost:4173",
    ]
    configured = resolve_cors_origins(
        logger=logger, default_dev_origins=local_defaults
    )
    if "*" in configured:
        configured = local_defaults
    return {
        origin.rstrip("/")
        for origin in configured
        if _origin_is_loopback(origin)
    }


def _origin_is_loopback(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return parsed.scheme in {"http", "https"} and _is_loopback_host(parsed.netloc)
    except ValueError:
        return False


def _install_auth_bypass() -> None:
    """AUTH_DISABLED 时覆盖鉴权依赖，让 admin 层路由不再要求 RBAC 角色。

    为什么覆盖而不是改上游：`require_admin` 由 router 在 import 期用
    `Depends(require_admin)` 钉进依赖树，FastAPI 的 dependency_overrides 仍会生效，
    且无需改动 `backend/services/api/user_app/**` 的原文（迁移镜像要保持可回灌）。
    """
    try:
        from backend.services.api.user_app.middleware.auth import (
            get_current_user,
            require_admin,
        )

        dev = _dev_identity
        app.dependency_overrides[require_admin] = dev
        app.dependency_overrides[get_current_user] = dev
        logger.warning(
            "AUTH_DISABLED=1 → 鉴权已关闭（含 admin 层），身份固定为 user_id=%s",
            dev()["user_id"],
        )
    except Exception as exc:
        # AUTH_DISABLED is a deployment-wide access decision. Starting with only
        # the HTTP middleware bypass installed would leave admin dependencies
        # protected (or inconsistently protected) while health reports no-auth.
        # Fail startup so the configured security model is never partially active.
        raise RuntimeError(
            "AUTH_DISABLED could not install all authentication dependency overrides"
        ) from exc


_runtime_environment = os.getenv("ENVIRONMENT", "development").strip().lower()
_NO_AUTH_ENVIRONMENTS = frozenset(
    {"development", "dev", "local", "test", "testing"}
)
if _env_flag("AUTH_DISABLED") and _runtime_environment not in _NO_AUTH_ENVIRONMENTS:
    raise RuntimeError(
        "AUTH_DISABLED is allowed only in a local development or test environment"
    )

if _env_flag("AUTH_DISABLED"):
    _install_auth_bypass()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    method = request.method.upper()

    # ── 免登录模式（AUTH_DISABLED=1）──────────────────────────────────
    # 独立部署定位：只有本机上的因子挖掘前端在访问，不接用户体系。
    # 直接注入一个管理员身份并放行；生产另起网关时不要开这个开关。
    if _env_flag("AUTH_DISABLED"):
        # No-auth mode grants every request the development admin identity. Require
        # both a loopback TCP peer and a loopback Host (Host alone is spoofable),
        # then reject cross-site/opaque origins on state-changing requests.
        if not _is_loopback_client(request):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "免登录模式仅接受本机连接"},
            )
        if not _is_loopback_host(request.headers.get("host")):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "免登录模式仅接受 localhost 请求"},
            )
        if method not in {"GET", "HEAD", "OPTIONS", "TRACE"}:
            origin = request.headers.get("origin")
            fetch_site = request.headers.get("sec-fetch-site", "").lower()
            if origin is not None:
                if origin == "null" or origin.rstrip("/") not in _allowed_local_origins():
                    return JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={"detail": "免登录模式拒绝非本机页面来源"},
                    )
            elif fetch_site == "cross-site":
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "免登录模式拒绝跨站写请求"},
                )
        request.state.user = dict(_dev_identity())
        return await call_next(request)

    try:
        from backend.shared.auth import AuthManager, get_internal_call_secret

        expected = get_internal_call_secret()
    except Exception:  # noqa: BLE001 - 拿不到密钥就 fail-closed
        expected = ""
        AuthManager = None  # type: ignore[assignment]

    internal = request.headers.get("X-Internal-Call")
    is_internal_call = bool(expected) and internal == expected

    if is_internal_call:
        user_id = request.headers.get("X-User-Id")
        tenant_id = request.headers.get("X-Tenant-Id", "default")
    else:
        user_id = None
        tenant_id = "default"

    # 没有内部信任头时，退回直连 JWT 校验（支持 Nginx / 本机直连）
    auth_header = request.headers.get("Authorization")
    if not user_id and auth_header and auth_header.startswith("Bearer ") and AuthManager:
        try:
            payload = AuthManager().verify_token(auth_header.split(" ", 1)[1])
            user_id = str(payload.get("sub") or payload.get("user_id") or "")
            tenant_id = str(payload.get("tenant_id") or "default")
        except Exception:  # noqa: BLE001 - 交由下游决定
            pass

    if (
        method != "OPTIONS"
        and path == "/metrics"
        and not is_internal_call
        and not user_id
    ):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Authentication required to read metrics"},
        )

    if method != "OPTIONS" and path.startswith("/api/v1/"):
        if not is_internal_call and not user_id:
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "detail": (
                        "Authentication required "
                        "(Invalid internal secret or missing user context)"
                    )
                },
            )
        if not user_id and any(path.startswith(p) for p in PROTECTED_PREFIXES):
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "需要登录。请确认您的登录状态并重试。"},
            )

    if user_id:
        # admin 身份规范化（与 engine/api 两侧同口径）
        try:
            from backend.shared.admin_identity import (
                is_admin_user_id,
                normalize_admin_user_id,
            )

            if is_admin_user_id(user_id):
                user_id = normalize_admin_user_id(user_id)
        except Exception:  # noqa: BLE001 - 规范化失败不阻断
            pass
        request.state.user = {"user_id": user_id, "tenant_id": tenant_id, "sub": user_id}

    return await call_next(request)


@app.middleware("http")
async def request_metrics_middleware(request: Request, call_next):
    """Record low-cardinality route metrics without exposing raw URL values."""
    if request.url.path == "/metrics":
        return await call_next(request)

    started = time.perf_counter()
    response_status = 500
    try:
        response = await call_next(request)
        response_status = response.status_code
        return response
    finally:
        route = request.scope.get("route")
        route_template = getattr(route, "path", "unmatched")
        if not isinstance(route_template, str):
            route_template = "unmatched"
        method = request.method.upper()
        method_label = method if method in _METRIC_HTTP_METHODS else "OTHER"
        status_label = (
            f"{response_status // 100}xx"
            if 100 <= response_status < 600
            else "other"
        )
        HTTP_REQUESTS.labels(
            method=method_label,
            route=route_template,
            status=status_label,
        ).inc()
        HTTP_REQUEST_DURATION.labels(
            method=method_label,
            route=route_template,
        ).observe(time.perf_counter() - started)


try:
    from backend.shared.cors import resolve_cors_origins

    # No-auth grants a shared admin identity. Never pair it with wildcard CORS:
    # a hostile website could read localhost API responses even if writes check Origin.
    cors_origins = (
        sorted(_allowed_local_origins())
        if _env_flag("AUTH_DISABLED")
        else resolve_cors_origins(logger=logger)
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception as e:  # noqa: BLE001
    logger.warning("CORS middleware skipped: %r", e)


# ── 可观测 ──────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    database_status = "ok"
    try:
        from sqlalchemy import text

        from backend.shared.database_manager_v2 import get_session

        async with asyncio.timeout(3):
            async with get_session(read_only=True) as session:
                await session.execute(text("SELECT 1"))
    except Exception as e:  # noqa: BLE001 - health only exposes failure class
        database_status = f"error:{type(e).__name__}"

    mount_ok = bool(_MOUNTED) and not _FAILED
    startup_ddl = _env_flag("ENABLE_STARTUP_DDL")
    critical_startup = (
        "sync_db_pool",
        "rd_agent_factors",
        "factor_backtest_tables",
        "mining_tasks",
        "factor_pool_tables",
    )
    startup_ok = not startup_ddl or all(
        str(_STARTUP.get(name, "")).startswith("ok") for name in critical_startup
    )
    healthy = mount_ok and database_status == "ok" and startup_ok
    SERVICE_HEALTH.labels(service="factor-mining-standalone").set(
        1 if healthy else 0
    )
    set_service_health("factor-mining-standalone", healthy)
    return {
        "status": "healthy" if healthy else "degraded",
        "service": "factor-mining-standalone",
        "checks": {
            "mounts": "ok" if mount_ok else "degraded",
            "database": database_status,
            "startup_ddl": "ok" if startup_ok else "degraded",
        },
        "mounted": _MOUNTED,
        "failed": redact_data(_FAILED),
        "startup_ddl": _env_flag("ENABLE_STARTUP_DDL"),
        "auth_disabled": _env_flag("AUTH_DISABLED"),
        "startup": redact_data(_STARTUP),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Expose Prometheus metrics; auth middleware protects non-local deployments."""
    return Response(
        content=generate_latest(_METRICS_REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )


@app.get("/")
async def root():
    if _WEB_INDEX.is_file() and (_WEB_DIST / "assets").is_dir():
        return FileResponse(_WEB_INDEX)
    return {
        "message": "Factor Mining Standalone is running",
        "mounted": _MOUNTED,
        "failed": redact_data(_FAILED),
    }


@app.get("/{frontend_path:path}", include_in_schema=False)
async def serve_workspace_route(frontend_path: str):
    """Serve the SPA for browser routes, while keeping unknown API paths as 404s."""
    if not _WEB_INDEX.is_file() or not (_WEB_DIST / "assets").is_dir():
        raise HTTPException(status_code=404, detail="Not Found")

    # Never turn API, websocket, docs, or metrics typos into a successful HTML page.
    if frontend_path == "api" or frontend_path.startswith(("api/", "ws/")):
        raise HTTPException(status_code=404, detail="Not Found")
    if frontend_path in {"docs", "redoc", "openapi.json", "health", "metrics"}:
        raise HTTPException(status_code=404, detail="Not Found")

    candidate = (_WEB_DIST / frontend_path).resolve()
    try:
        candidate.relative_to(_WEB_DIST)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not Found") from None

    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(_WEB_INDEX)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8001, access_log=False)
