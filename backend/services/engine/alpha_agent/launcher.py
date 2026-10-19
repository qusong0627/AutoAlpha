"""AlphaAgent / RD-Agent 因子挖掘任务启动器

支持两种模式:
1. Legacy AlphaAgent (market=a_share, 使用 alphaagent/)
2. RD-Agent 多市场 (market=a_share|crypto|hong_kong|us_stock, 使用 rdagent/ + market_adapters/)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # 运行期不 import：task_store 拉 DB 栈，launcher 单测不碰库
    from backend.services.engine.alpha_agent.task_store import DuplicateActiveTask

from backend.services.engine.alpha_agent.algorithm_registry import (
    PROGRESS_KIND_JSONL,
    PROGRESS_KIND_WORKDIR,
    REPO_ROOT as _ALGO_REPO_ROOT,
    AlgorithmError,
    MiningAlgorithm,
    load_algorithm,
    normalize_algorithm,
    resolve_algorithm,
)
from backend.services.engine.alpha_agent.data_snapshot import snapshot_data_versions
from backend.services.engine.alpha_agent.mining_config import (
    ENV_FREQ,
    ENV_TASK_FINGERPRINT,
    FREQ_MINUTE,
    Window,
    normalize_freq,
    parse_window,
    resolve_windows,
    task_env,
)
from backend.services.engine.alpha_agent.task_fingerprint import (
    canonical_params,
    fingerprint_of,
)
from backend.services.engine.rd_agent.mining_datasets import (
    SUPPORTED_DATASETS,
    resolve_datasets,
)
from backend.shared.safe_pickle import load_pickle
from backend.shared.secret_redaction import (
    copy_redacted_stream,
    redact_data,
    redact_text,
)
from backend.shared.utc_datetime import UTC, to_utc_iso

logger = logging.getLogger(__name__)


def _linux_process_identity(pid: int) -> tuple[int, str] | None:
    """Return (process_group, start_ticks) for a PID without trusting PID reuse."""
    try:
        stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = stat_line[stat_line.rfind(")") + 1 :].split()
        return int(fields[2]), fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _stream_redacted_child_output(
    pipe: Any, log_file: Any, child_env: dict[str, str], task_id: str
) -> None:
    """Drain a child pipe without ever writing known credentials to its log file."""
    try:
        copy_redacted_stream(pipe, log_file, extra_env=child_env)
    except Exception:
        logger.exception("Failed to redact subprocess output for task %s", task_id)
        # Continue draining after a writer failure so the child cannot deadlock
        # against a full pipe. Remaining output is intentionally discarded.
        try:
            while pipe.read(65536):
                pass
        except Exception:
            pass
    finally:
        try:
            pipe.close()
        except Exception:
            pass
        try:
            log_file.close()
        except Exception:
            pass


def _sanitize_common_logs(task_log_dir: Path, child_env: dict[str, str]) -> int:
    """Redact RD-Agent's directly-written common_logs files after the runner exits.

    RD-Agent configures its own file handlers, so those bytes bypass the parent's
    stdout redactor. Rewrite each regular file through a private sibling temp file
    and atomically replace it; symlinks and non-regular files are ignored.
    """
    sanitized = 0
    for log_path in task_log_dir.rglob("common_logs.log"):
        temp_path: Path | None = None
        try:
            info = log_path.lstat()
            if not stat.S_ISREG(info.st_mode):
                continue

            temp_path = log_path.with_name(
                f".{log_path.name}.{uuid.uuid4().hex}.redacting"
            )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(temp_path, flags, 0o600)
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                read_fd = os.open(log_path, read_flags)
            except Exception:
                os.close(fd)
                raise
            if not stat.S_ISREG(os.fstat(read_fd).st_mode):
                os.close(read_fd)
                os.close(fd)
                temp_path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "wb") as writer, os.fdopen(read_fd, "rb") as reader:
                copy_redacted_stream(reader, writer, extra_env=child_env)
            # Keep the file private even if RD-Agent created it with permissive mode.
            os.chmod(temp_path, 0o600)
            os.replace(temp_path, log_path)
            sanitized += 1
        except Exception:
            logger.warning("Could not sanitize RD-Agent log %s", log_path)
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    return sanitized


def _remove_litellm_settings(task_log_dir: Path) -> int:
    """Remove RD-Agent's serialized LLM settings after the child has exited.

    These snapshots can contain API credentials copied from the child environment.
    They are runtime configuration artifacts, not mining results, and are not read
    by the AutoAlpha launcher. Never deserialize them to redact individual fields.
    """
    removed = 0
    try:
        task_info = task_log_dir.lstat()
        if not stat.S_ISDIR(task_info.st_mode):
            return removed
        parent = task_log_dir.parent.resolve(strict=True)
        root = task_log_dir.resolve(strict=True)
    except OSError:
        return removed
    if not root.is_relative_to(parent):
        logger.warning("Skipping out-of-root task directory during LLM artifact cleanup")
        return removed

    for settings_dir in root.rglob("LITELLM_SETTINGS"):
        try:
            info = settings_dir.lstat()
            if not stat.S_ISDIR(info.st_mode):
                continue
            resolved = settings_dir.resolve(strict=True)
            if not resolved.is_relative_to(root):
                logger.warning("Skipping out-of-task LLM settings path")
                continue
            shutil.rmtree(settings_dir)
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning(
                "Could not remove RD-Agent LLM settings directory (%s)",
                type(exc).__name__,
            )
    return removed


def _linux_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def _same_process(pid: Any, start_ticks: Any, boot_id: Any) -> bool:
    if os.name != "posix" or not pid or not start_ticks or boot_id != _linux_boot_id():
        return False
    try:
        identity = _linux_process_identity(int(pid))
    except (TypeError, ValueError):
        return False
    return identity is not None and identity[1] == str(start_ticks)


def _stop_persisted_process_group(data: dict[str, Any]) -> bool:
    """Stop a task's process group only when its persisted PID identity still matches."""
    pid = data.get("runner_pid")
    pgid = data.get("runner_pgid")
    try:
        if not pid or not pgid or int(pgid) != int(pid):
            return False
    except (TypeError, ValueError):
        return False
    if not _same_process(pid, data.get("runner_start_ticks"), data.get("boot_id")):
        return False
    try:
        os.killpg(int(pgid), signal.SIGTERM)
        for _ in range(20):
            try:
                os.killpg(int(pgid), 0)
            except ProcessLookupError:
                return True
            time.sleep(0.1)
        os.killpg(int(pgid), signal.SIGKILL)
        return True
    except ProcessLookupError:
        return False
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Could not stop stale mining process group %s: %s", pgid, exc)
        return False


async def _terminate_task_process_group(
    process: subprocess.Popen,
    *,
    grace_seconds: float = 5.0,
) -> None:
    """Stop a task's session, including descendants that outlive its leader."""
    pgid = process.pid  # runner Popen uses start_new_session=True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        if process.poll() is None:
            process.terminate()
    except OSError:
        if process.poll() is None:
            process.terminate()

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        except OSError:
            break
        await asyncio.sleep(0.1)

    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        if process.poll() is None:
            process.kill()
    except OSError:
        if process.poll() is None:
            process.kill()
    if process.poll() is None:
        await asyncio.to_thread(process.wait)


async def _snapshot_data_versions_for_task(
    *, market: str, freq: str | None, datasets: Sequence[str]
) -> dict[str, Any]:
    """Capture optional data provenance without blocking the API event loop.

    QuantDB may live on a Windows-mounted 9p filesystem, where walking every
    selected dataset partition can take minutes. Provenance is useful for
    audit, but must not prevent a mining task from being created or freeze the
    API while that walk is in progress.
    """
    try:
        timeout = max(0.1, float(os.getenv("ALPHA_AGENT_DATA_SNAPSHOT_TIMEOUT", "5")))
    except (TypeError, ValueError):
        timeout = 5.0
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(
                snapshot_data_versions,
                market=market,
                freq=freq,
                datasets=datasets,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "data-version snapshot exceeded %.1fs; allowing task creation to continue",
            timeout,
        )
        return {
            "snapshot_at": to_utc_iso(datetime.now(timezone.utc)),
            "market": market,
            "freq": freq or "day",
            "provider": {"name": "quantdb"} if market == "a_share" else None,
            "datasets": {},
            "minute": None,
            "note": "数据版本快照超时；任务已先行启动，快照未完整记录",
            "errors": [f"数据版本快照超过 {timeout:g} 秒，未阻止任务启动"],
        }


def _epoch_iso(ts: Any) -> str | None:
    """epoch 秒 → ISO-8601 UTC（带 Z）；取不到就是 None。

    `created_at` 在 `EvolutionTask` 里是 `time.time()` 的 epoch 秒。前端那个全局进度
    面板靠它回答「这个任务是刚刚起的、还是我去吃饭前就挂着的那一个」，所以**绝不能**
    在缺失时回落到 `now()`——那会把一个几小时前的僵尸任务显示成刚提交的。同一纪律
    适用于 runner 进度事件的 `ts`（取不到就是不写 end_time，不伪造时刻）。
    """
    try:
        return to_utc_iso(datetime.fromtimestamp(float(ts), UTC))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    QUEUED = "queued"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class QueueFullError(RuntimeError):
    """排队深度已达上限：批量派发路径据此逐卡片回可操作错误（不落任务行）。"""


#: 去重（移植 P3）认定的「活跃」：与 task_store.ACTIVE_STATUSES 同口径——
#: queued 也算「这个配置还在路上」，同配置再提交应当复用既有任务而不是再排一个。
#: 与并发上限的活跃集（pending/running）**不是**一回事：容量不数排队任务，
#: 去重必须数（否则同一配置能排进队列 N 次）。
_ACTIVE_FOR_DEDUPE = (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING)


#: 并发/排队上限的环境变量名与默认值（**唯一读取点**：路由 429 判定与排队
#: 判断共用 :meth:`AlphaAgentLauncher.running_capacity` / ``queue_capacity``）。
ENV_MAX_RUNNING_PER_USER = "ALPHA_AGENT_MAX_RUNNING_PER_USER"
ENV_MAX_RUNNING_GLOBAL = "ALPHA_AGENT_MAX_RUNNING_GLOBAL"
ENV_MAX_QUEUED_PER_USER = "ALPHA_AGENT_MAX_QUEUED_PER_USER"
ENV_MAX_QUEUED_GLOBAL = "ALPHA_AGENT_MAX_QUEUED_GLOBAL"
# Local single-user deployment defaults: one RD-Agent run can saturate the
# available CPU/memory even when the minimum hardware probe is bypassed for dev.
# Operators may raise these explicitly after measuring the actual workload.
DEFAULT_MAX_RUNNING_PER_USER = 1
DEFAULT_MAX_RUNNING_GLOBAL = 1
DEFAULT_MAX_QUEUED_PER_USER = 20
DEFAULT_MAX_QUEUED_GLOBAL = 50


#: 排空时的 LLM 配置重解析器（注册制，engine 启动期由路由注册）。
#: 排队行**绝不持久化任何密钥**——排空时按 (user_id, tenant_id) 重新解析。
#: 未注册（主机直跑/单测）→ None 覆盖，容器 env 兜底照旧；注册后返回 None
#: （或抛异常）→ 任务显式失败并给可操作报错，绝不静默换供应商。
_llm_override_resolver: (
    Callable[[str, str], Awaitable[dict[str, str] | None]] | None
) = None


def set_llm_override_resolver(
    fn: Callable[[str, str], Awaitable[dict[str, str] | None]] | None,
) -> None:
    global _llm_override_resolver
    _llm_override_resolver = fn


#: 排空时服务端环境也没有 Key：与 evolve 端点 412 同一口径。
_QUEUE_LLM_MISSING_MESSAGE = (
    "排队任务启动前重解析 LLM 配置失败：未配置 LLM API Key"
    "（请在受保护的服务端环境中设置 DEEPSEEK_API_KEY、AI_IDE_LLM_API_KEY、"
    "AI_IDE_API_KEY 或 OPENAI_API_KEY）。"
    "排队不持久化密钥，无法沿用提交时的配置。"
)


# 进度落库节流：内存心跳每 3s，DB 写 ≥15s 一次（轮询写库会把库压成热点）。
_DB_SYNC_INTERVAL_S = 15.0
_DB_TERMINAL_WRITE_ATTEMPTS = 3


def _task_store():
    """任务记录层 store（模块级函数便于测试替换；失败语义见 _db_* 系列）。"""
    from backend.services.engine.alpha_agent.task_store import get_mining_task_store

    return get_mining_task_store()


#: 任务日志根：每任务一个子目录（subprocess_stdout.log + task_state.json +
#: RD-Agent 工作区）。默认 /data——容器重建不丢（T-FM-20）；历史本体另在
#: PG `rd_agent_mining_tasks`，日志只是排障补充。主机直跑等 /data 不可写的
#: 场景用 LOG_TRACE_PATH 显式指到可写目录（旧默认即 /tmp/alpha_agent_logs）。
DEFAULT_LOG_DIR = "/data/alpha_agent_logs"

#: 终态任务日志目录的留存线（天）：engine 启动时由 :func:`gc_task_logs` 执行。
ENV_LOG_RETENTION_DAYS = "LOG_TRACE_RETENTION_DAYS"
DEFAULT_LOG_RETENTION_DAYS = 90

#: 可被 GC 的 task_state.json status 原文；认不出的状态与非终态一律不动。
_GC_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _resolve_log_dir() -> Path:
    return Path(os.getenv("LOG_TRACE_PATH", DEFAULT_LOG_DIR))


def _secure_log_root(path: Path) -> None:
    """Create/protect the private task log root and its first-level directories.

    RD-Agent writes prompts, workspaces, and pickle artifacts below this tree. A
    private root prevents other local accounts from reading old files even when
    third-party writers create those files with their default 0644 mode.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"任务日志根必须是普通目录，不能是符号链接: {path}")
    if os.name != "posix":
        return

    os.chmod(path, 0o700)
    with os.scandir(path) as entries:
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                os.chmod(entry.path, 0o700)


def _secure_task_log_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"任务日志目录必须是普通目录，不能是符号链接: {path}")
    if os.name == "posix":
        os.chmod(path, 0o700)


def _strip_unneeded_child_secrets(
    env: dict[str, str],
    *,
    needs_llm: bool,
    needs_factor_name_llm: bool,
    needs_database: bool = False,
) -> None:
    """Remove credentials the mining runner does not need.

    LLM and optional factor-name providers receive only their selected key(s).
    Database access is retained only for the deterministic statistical runner.
    The LLM runner executes generated Python and persists its result in the API
    process after exit.
    """
    llm_keys: set[str] = set()
    if needs_llm:
        llm_keys.update(
            {
                "OPENAI_API_KEY",
                "LITELLM_OPENAI_API_KEY",
                "EMBEDDING_API_KEY",
                "QMF_FACTOR_NAME_API_KEY",
            }
        )
    elif needs_factor_name_llm:
        llm_keys.update({"OPENAI_API_KEY", "QMF_FACTOR_NAME_API_KEY"})
    exact_secret_names = {
        "DATABASE_URL",
        "INTERNAL_CALL_SECRET",
        "SECRET_KEY",
        "JWT_SECRET",
        "JWT_SECRET_KEY",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
        "DEEPSEEK_API_KEY",
        "AI_IDE_API_KEY",
        "AI_IDE_LLM_API_KEY",
    }
    for key in tuple(env):
        if key in llm_keys:
            continue
        upper_key = key.upper()
        is_database_key = (
            upper_key == "DATABASE_URL"
            or upper_key.startswith(("DB_", "POSTGRES_"))
            or upper_key == "PGPASSWORD"
        )
        if needs_database and is_database_key:
            continue
        normalized_key = "".join(char for char in upper_key if char.isalnum())
        if (
            upper_key in exact_secret_names
            or is_database_key
            or upper_key.endswith(
                (
                    "_API_KEY", "_TOKEN", "_SECRET", "_SECRET_KEY", "_PASSWORD",
                    "_MASTER_KEY", "_PRIVATE_KEY", "_SIGNING_KEY", "_CREDENTIAL",
                    "_CREDENTIALS", "_CREDENTIAL_FILE",
                )
            )
            or "CREDENTIAL" in normalized_key
            or upper_key == "NETRC"
            # Tencent COS and AWS-style credentials use names such as
            # ``TENCENT_SECRET_ID`` / ``AWS_ACCESS_KEY_ID``; legacy SDKs also
            # accept camel-case ``SecretId``. Normalize separators and case so
            # these are not inherited by generated-code runners.
            or normalized_key.endswith(
                ("SECRETID", "SECRETACCESSKEY", "ACCESSKEYID", "CLIENTSECRET")
            )
        ):
            env.pop(key, None)


def _env_int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r 不是整数，按默认 %d 处理", name, raw, default)
        return default


def gc_task_logs(
    log_dir: Path | None = None, *, retention_days: int | None = None
) -> dict[str, int]:
    """任务日志留存 GC：清理终态且超龄的任务目录，返回 ``{"scanned", "pruned"}``。

    只认「``<log_dir>/<task_id>/task_state.json`` 可解析 + status 是终态 +
    状态文件 mtime 早于留存线」的目录；任何一条不满足就原样保留——认不出的
    东西不动（框架自身往根下写的 ``__session__`` 之类天然免疫）。历史本体在
    PG ``rd_agent_mining_tasks``，清掉的是排障日志，清后「查看日志」读不到
    属预期留存语义。``retention_days`` 缺省读 env，0 = 下次启动清空终态。
    """
    root = Path(log_dir) if log_dir is not None else _resolve_log_dir()
    if retention_days is None:
        retention_days = _env_int(ENV_LOG_RETENTION_DAYS, DEFAULT_LOG_RETENTION_DAYS)
    cutoff = time.time() - max(0, int(retention_days)) * 86400
    _secure_log_root(root)
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return {"scanned": 0, "pruned": 0}
    scanned = 0
    pruned = 0
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        scanned += 1
        state_file = entry / "task_state.json"
        if state_file.is_symlink():
            continue
        try:
            with open(state_file) as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if str(data.get("status", "")) not in _GC_TERMINAL_STATUSES:
            continue
        try:
            if state_file.stat().st_mtime > cutoff:
                continue
        except OSError:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.warning("Failed to prune expired task log %s: %s", entry.name, exc)
            continue
        pruned += 1
    return {"scanned": scanned, "pruned": pruned}


@dataclass(frozen=True)
class QueueReceipt:
    """一次提交的落点：已启动 or 已排队（带本用户队列内的位次）。"""

    task_id: str
    status: str  # "running"（已派发子进程）| "queued"
    queue_position: int | None = None
    #: 命中去重（移植 P3）：本次提交**没有新建任务**——上面的 task_id/status/
    #: 位次都是既有任务的实况，调用方据此提示「同配置已在跑/已在排」。
    deduplicated: bool = False


@dataclass(frozen=True)
class MinuteScope:
    """分钟挖掘的可用范围（分钟 provider 事实 → 配置层口径，单一出处）。

    ``usable`` 是**末端连续块**（跨过数据空洞的那一段），它就是分钟模式的默认
    挖掘窗与允许区间；``data_end`` 直接取 ``usable.end``——provider 的 day 层比
    1min 层更长（含次日与更长历史），qlib 回测的 t+1 取数天然满足，不需要像日线
    那样再退让一天。``coverage`` 是 provider 全区间（含断层之前），只在报错消息
    里用来区分"超覆盖"与"跨断层"。
    """

    usable: Window
    coverage: Window
    days: int
    gaps: tuple[tuple[date, date], ...] = ()

    @property
    def data_end(self) -> date:
        """回测可用末端 = 可用块终点（day 层已留 t+1 缓冲）。"""
        return self.usable.end

    def gap_text(self) -> str:
        """空洞摘要（错误消息用）：``2026-01-06 ~ 2026-03-04``，多段顿号连接。"""
        return "、".join(f"{a.isoformat()} ~ {b.isoformat()}" for a, b in self.gaps)


def minute_scope() -> MinuteScope | None:
    """读分钟 provider 的可用范围（未构建回 ``None``，不抛）。

    未构建在这里不抛：状态/配置接口要能如实回 ``ready=false``；挖掘入口
    （:func:`normalize_mining_config`）把它转成显式 ValueError（→ 400）。
    """
    from backend.services.engine import minute_qlib_provider as MP

    facts = MP.minute_data_facts(MP.resolve_minute_provider_dir())
    if facts is None:
        return None
    return MinuteScope(
        usable=Window(facts.usable_start, facts.coverage.end),
        coverage=Window(facts.coverage.start, facts.coverage.end),
        days=facts.usable_days,
        gaps=facts.gaps,
    )


def require_minute_window(
    window: Window | None, scope: MinuteScope, *, label: str
) -> None:
    """分钟窗口必须整体落在末端连续块内（覆盖内但跨断层/落在断层之前也拒）。"""
    if window is None:
        return
    if window.start >= scope.usable.start and window.end <= scope.usable.end:
        return
    gaps = scope.gap_text()
    raise ValueError(
        f"{label} {window.start.isoformat()} ~ {window.end.isoformat()}"
        f" 不在分钟可用连续块内（数据断层：{gaps or '无'}）；可用区间 "
        f"{scope.usable.start.isoformat()} ~ {scope.usable.end.isoformat()}"
        f"，共 {scope.days} 个交易日"
    )


def normalize_mining_config(
    mining_start: str | None,
    mining_end: str | None,
    valid_start: str | None,
    valid_end: str | None,
    datasets: Sequence[str] | None,
    *,
    freq: str | None = None,
    market: str = "a_share",
    coverage: Window | None = None,
    data_end: date | None = None,
) -> MiningConfig:
    """校验并归一自定义挖掘配置（ISO 字符串进出，ValueError → 路由回 400）。

    **所有创建路径共用这一处**（evolve / 批量派发 / 队列补位），避免出现
    「evolve 校验了、其它入口没校验」的旁路。语义：

    - 挖掘区间两端都空 = 不指定（默认窗口）；只填一端即拒。
    - 检验区间两端都空 = 不启用；只填一端、或与挖掘区间重叠即拒（样本外）。
    - 检验区间不得单独存在——没有挖掘窗就无从判定"样本外"。
    - 数据集空/None = 全选（迁移前行为）；未登记 key 即拒；**分钟模式显式拒**
      （1min 数据面只有 OHLCV/amount/vwap，不叠日线富化列——静默忽略会让用户
      以为选的数据集生效了）。
    - ``freq`` 空/None = 日线（**迁移前行为逐位一致**）；``"1min"`` = 分钟原生
      挖掘（只有 A 股有分钟数据底座，其它市场显式拒——见
      ``mining_config.normalize_freq``）。
    - ``coverage`` 给定时两个窗口都须落在数据覆盖内（路由层从 QuantDB
      分区目录现读）；不给（数据源不可用/排队补位）不拦——不做假校验。
    - ``data_end``（回测可用末端）给定时两个窗口终点退让到该日——终点压在
      数据末端最后一根 bar 上会让 qlib 回测崩在 t+1 时间步（见
      ``mining_config.clamp_to_data_end``）。任务行因此存的是**可跑的**端点。
    - **分钟模式**：调用方传的日线覆盖/末端保护一律作废，改用 provider 的可用
      连续块（日线覆盖 2016→2026 与 1min 断层不是一回事）；不指定窗口时默认
      窗 = 可用连续块（分钟只有 ~4 个月数据，4+1 年默认窗毫无意义）。

    返回 :class:`MiningConfig`；未指定的部分回空串/全选元组/空频率。
    """
    freq_value = normalize_freq(freq, market=market)
    scope: MinuteScope | None = None
    if freq_value == FREQ_MINUTE:
        scope = minute_scope()
        if scope is None:
            raise ValueError(
                "分钟数据面未构建或不可读：请先构建 1min provider"
                "（backend.services.engine.minute_qlib_provider）"
            )
        if datasets and [d for d in datasets if str(d or "").strip()]:
            raise ValueError(
                "分钟挖掘不支持数据集选择（1min 数据面只有 OHLCV/amount/vwap，"
                "不叠加日线富化列）；请清空数据集选项或切回日线"
            )
        coverage, data_end = scope.coverage, scope.data_end

    mining, validation = resolve_windows(
        mining_start,
        mining_end,
        valid_start,
        valid_end,
        coverage=coverage,
        data_end=data_end,
    )
    if scope is not None:
        if mining is None:
            mining = scope.usable  # 分钟默认窗 = 可用连续块
        require_minute_window(mining, scope, label="挖掘数据区间")
        require_minute_window(validation, scope, label="因子检验区间")
    resolved = () if scope is not None else resolve_datasets(datasets)
    # 分钟任务不落数据集（上面已拒显式选择；空元组 = 任务行/子进程 env 都不留痕，
    # 不把"全选"这个日线语义伪装成分钟任务的配置）
    if freq_value == "day":
        freq_value = ""  # 未指定 = 日线：任务行不留参数痕迹
    if mining is None:
        return MiningConfig(datasets=resolved, freq=freq_value)
    v_start, v_end = validation.iso() if validation else ("", "")
    return MiningConfig(*mining.iso(), v_start, v_end, resolved, freq_value)


@dataclass(frozen=True)
class MiningConfig:
    """已校验的挖掘配置（任务行/子进程 env 的唯一来源）。

    空串语义：窗口空 = 未指定（默认窗口路径）；``freq`` 空 = 日线（与迁移前
    行为一致）；``datasets`` 全选元组 = 未限定。
    """

    mining_start: str = ""
    mining_end: str = ""
    valid_start: str = ""
    valid_end: str = ""
    datasets: tuple[str, ...] = SUPPORTED_DATASETS
    #: 挖掘频率：``""``（日线，默认）/ ``"1min"``（分钟原生挖掘，仅 A 股）
    freq: str = ""


@dataclass
class EvolutionTask:
    task_id: str
    user_id: str
    tenant_id: str = "default"
    market: str = "a_share"
    data_source: str = ""
    universe: str = "csi300"
    direction: str = ""
    #: 方向模式（T-MV-02）：'selected'/'random'=类别选择路径；''=模式未参与
    #: （自由文本/卡片派发）→ 落库为 NULL，历史页如实呈现
    direction_mode: str = ""
    #: 加权抽样的复现凭证（T-MV-03，JSON 文本）：seed/候选/权重/命中；
    #: ''=没抽样（自由文本、selected、抽样降级）→ 落库为 NULL
    direction_meta: str = ""
    #: 入库闸门请求意图（T-MV-05）：'off'/'soft'/'hard'；''=未指定
    #: （入池时再叠 env ALPHA_GATE_MODE 解析生效模式）→ 落库为 NULL
    quality_gate_mode: str = ""
    #: 自定义挖掘数据区间（ISO，移植 P1）：''=未指定 → 默认窗口
    #: （行为与迁移前逐位一致）。非空时经 task_env 注入子进程，驱动 h5 数据面、
    #: 逐因子 IC 评估与 qlib 实验分段。
    mining_start: str = ""
    mining_end: str = ""
    #: 因子检验区间（样本外，ISO）：''=未启用（无检验段，实验按挖掘窗三分）
    valid_start: str = ""
    valid_end: str = ""
    #: 挖掘数据集选择（登记序，如 ("features_daily","l1_factors")）：()=全选
    datasets: tuple[str, ...] = ()
    #: 挖掘频率（移植 P1.5）：''=日线（默认，行为与迁移前逐位一致）
    #: / '1min'=分钟原生挖掘（数据面换 1min h5 + conf 换分钟 provider，仅 A 股）
    freq: str = ""
    #: 挖掘算法 key（移植 P2）：**已解析**的 manifest key（''=未指定由入口
    #: resolve 成默认 rdagent）。运行时能力（runner/uses_llm/requires_gpu/
    #: stages）一律从 manifest 现读——这里只存身份，不复制能力面。
    algorithm: str = ""
    #: 去重指纹（移植 P3）：决定这次挖掘跑什么的请求参数规范哈希（见
    #: ``task_fingerprint``）。''=没算（迁移前写盘的 state）→ 不参与去重
    fingerprint: str = ""
    #: 提交时刻的参数快照（移植 P3）：指纹入参的规范字典原样留档（任务行 JSON）
    params_snapshot: dict[str, Any] | None = None
    #: 提交时刻的数据版本快照（移植 P3）：这份结果是在哪份数据上挖的——
    #: 数据日更后回看历史任务，不能拿今天的覆盖解释昨天的结果（见 data_snapshot）
    data_versions: dict[str, Any] | None = None
    status: TaskStatus = TaskStatus.PENDING
    progress: str = ""
    phase: str = "pending"
    progress_pct: int = 0
    loop_n: int = 5
    current_loop: int = 0
    created_at: float = field(default_factory=time.time)
    error_message: str | None = None
    result: dict[str, Any] | None = None
    process: subprocess.Popen | None = None
    _cancel_requested: bool = False
    timeline: list[dict[str, Any]] = field(default_factory=list)
    token_usage: dict[str, Any] = field(default_factory=dict)
    # 上次进度落库时刻（epoch 秒）。3s 内存心跳不变，DB 写节流见 _DB_SYNC_INTERVAL_S。
    _last_db_sync: float = 0.0


def _build_runner_cmd(
    task: EvolutionTask,
    algo: MiningAlgorithm,
    *,
    loop_n: int,
    direction: str,
    log_dir: Path,
) -> list[str]:
    """子进程命令行的唯一构造点（runner 由 manifest 声明，参数按能力面裁剪）。

    ``--loop-n``/``--direction`` 只发给消费它们的 runner（``uses_llm``）：统计派
    argparse 不认这两个旗标，无条件带上会让子进程在解析参数时就退出（exit 2）。
    单测直接钉这里，不必真起子进程。
    """
    cmd = [
        sys.executable,
        "-X",
        "faulthandler",  # 段错误时输出 Python traceback 便于定位
        str(_ALGO_REPO_ROOT / algo.runner),
        "--task-id",
        task.task_id,
        "--user-id",
        task.user_id,
        "--log-dir",
        str(log_dir),
        "--universe",
        task.universe,
        "--market",
        task.market,
    ]
    if algo.uses_llm:
        cmd += ["--loop-n", str(loop_n), "--direction", direction]
    return cmd


def _fingerprint_params(
    *,
    algo_key: str,
    market: str,
    universe: str,
    data_source: str | None,
    direction: str | None,
    direction_mode: str | None,
    quality_gate_mode: str | None,
    loop_n: int,
    cfg: MiningConfig,
    source: str,
    doc_id: str | None,
) -> dict[str, Any]:
    """指纹入参的规范字典（**两条提交路径的唯一构造点**：指纹与快照同源）。

    ``cfg`` 必须是 :func:`normalize_mining_config` 的产物——用归一后的值算，
    「任务行登记的窗口/数据集」与「指纹哈希的内容」才是同一份，事后能从
    任务行的 params_snapshot 自证为什么这次没去重。``algo_key`` 同理：传已
    resolve 的 manifest key，不传请求原文。
    """
    return canonical_params(
        algorithm=algo_key,
        market=market,
        universe=universe,
        data_source=data_source,
        direction=direction,
        direction_mode=direction_mode,
        quality_gate_mode=quality_gate_mode,
        loop_n=loop_n,
        freq=cfg.freq,
        mining_start=cfg.mining_start,
        mining_end=cfg.mining_end,
        valid_start=cfg.valid_start,
        valid_end=cfg.valid_end,
        datasets=cfg.datasets,
        source=source,
        doc_id=doc_id,
    )


def _json_text(value: Any) -> str:
    """快照 dict → JSON 文本（任务行 TEXT 列）；空/None → ''（落 NULL）。"""
    if not value:
        return ""
    return json.dumps(value, ensure_ascii=False, default=str)


def _progress_kind_of(algorithm_key: str | None) -> str:
    """任务行 → 进度面形状（现读 manifest，不落任务行：能力面只存身份）。

    读不到（老任务 key 合法但 manifest 被删/改坏）按 ``workdir`` 兜底——
    那是迁移前的唯一形状，前端据此退化显示不报错。
    """
    try:
        return load_algorithm(normalize_algorithm(algorithm_key)).progress_kind
    except AlgorithmError:
        return PROGRESS_KIND_WORKDIR


class AlphaAgentLauncher:
    """挖掘任务启动器：runner 由算法 manifest 声明（不再有硬编码单一路径）。"""

    def __init__(self) -> None:
        self._tasks: dict[str, EvolutionTask] = {}
        self._log_dir = _resolve_log_dir()
        _secure_log_root(self._log_dir)
        # Only drain rows whose DB lease this process created or explicitly claimed.
        self._owned_queued_task_ids: set[str] = set()
        # 排空重入闸：并发 drain（收尾触发 vs 清扫器）只允许一个在跑
        self._draining = False
        self._background_naming_tasks: set[asyncio.Task[Any]] = set()
        self._load_tasks()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start_evolution(
        self,
        user_id: str,
        *,
        market: str = "a_share",
        universe: str = "csi300",
        loop_n: int = 5,
        seed: str | None = None,
        provider_uri: str | None = None,
        direction: str | None = None,
        direction_mode: str | None = None,
        direction_meta: str | None = None,
        quality_gate_mode: str | None = None,
        data_source: str | None = None,
        mining_start: str | None = None,
        mining_end: str | None = None,
        valid_start: str | None = None,
        valid_end: str | None = None,
        datasets: Sequence[str] | None = None,
        freq: str | None = None,
        algorithm: str | None = None,
        source: str = "text",
        doc_id: str | None = None,
        llm_overrides: dict[str, str] | None = None,
        tenant_id: str = "default",
    ) -> str:
        """Start a factor evolution task. Returns task_id.

        source/doc_id: 输入来源（text=文字指令，doc=文档解析链），落任务记录行。
        direction_mode: 方向如何被选中（'selected'/'random'）；None=模式未参与，
        落库为 NULL——调用方只在类别选择真正发生时传值（T-MV-02）。
        direction_meta: 加权抽样的复现凭证（JSON 文本，T-MV-03）；None=没抽样。
        quality_gate_mode: 入库闸门请求意图（'off'/'soft'/'hard'，T-MV-05）；
        None=未指定，落库为 NULL（入池时由 resolve_admission_gate_mode 解析）。
        mining_*/valid_*/datasets/freq: 自定义挖掘配置（移植 P1/P1.5），
        语义与校验见 :func:`normalize_mining_config`；非法配置抛 ValueError
        （由路由翻成 400），绝不静默退回默认窗口。
        algorithm: 挖掘算法 key（移植 P2）；None/''=默认 rdagent（迁移前唯一
        路径，行为逐位一致）。经算法注册表全量闸门（存在→启用→市场→频率→
        数据集），非法组合抛 :class:`AlgorithmError`（路由翻 400）——绝不
        「按默认算法跑出一份不是用户要的东西」。已解析的 key 落任务行，
        运行期能力面（runner/uses_llm/requires_gpu）从 manifest 现读。
        llm_overrides: 部署环境中的 LLM 环境变量覆盖，
        优先于容器全局 env 注入子进程（``uses_llm=false`` 的算法不注入）。

        **永不排队**：并发上限的 429 背压契约在路由层（前端「原文上屏」）；
        需要「满了自动排队」的批量派发路径走 :meth:`start_or_queue`。
        **同配置去重（P3）**：同用户已有**活跃**的同指纹任务时，不新建——
        直接返回既有 task_id（双击「开始挖掘」、前端重放都不该再烧一份
        token/GPU）。任务收终态后同配置重跑是正当行为，不拦。
        """
        from backend.services.engine.alpha_agent.hw_lock import (
            assert_factor_mining_hardware,
        )

        algo = resolve_algorithm(
            algorithm, market=market, freq=freq, datasets=list(datasets or [])
        )
        cfg = normalize_mining_config(
            mining_start,
            mining_end,
            valid_start,
            valid_end,
            datasets,
            freq=freq,
            market=market,
        )
        params = _fingerprint_params(
            algo_key=algo.key,
            market=market,
            universe=universe,
            data_source=data_source,
            direction=direction,
            direction_mode=direction_mode,
            quality_gate_mode=quality_gate_mode,
            loop_n=loop_n,
            cfg=cfg,
            source=source,
            doc_id=doc_id,
        )
        fingerprint = fingerprint_of(params)
        existing = self.find_active_duplicate(user_id, fingerprint)
        if existing is not None:
            logger.info(
                "[alpha-agent] 同配置活跃任务复用：user=%s task=%s",
                user_id,
                existing.task_id,
            )
            return existing.task_id

        # 硬件锁按能力面收窄：统计派纯 CPU（占用的是 CPU 不是显卡），
        # 拿 GPU 锁拦它是错的——判据是 manifest 的 requires_gpu，不是算法名。
        # 顺序有意如此：去重先判——「同一配置已经在跑」比「硬件忙」更接近
        # 用户想问的问题（占着 GPU 的往往正是那个任务）。
        if algo.requires_gpu:
            assert_factor_mining_hardware()

        task_id = uuid.uuid4().hex[:16]
        task = EvolutionTask(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id or "default",
            market=market,
            universe=universe,
            loop_n=loop_n,
            data_source=data_source or "",
            direction=direction or "",
            direction_mode=direction_mode or "",
            direction_meta=direction_meta or "",
            quality_gate_mode=quality_gate_mode or "",
            mining_start=cfg.mining_start,
            mining_end=cfg.mining_end,
            valid_start=cfg.valid_start,
            valid_end=cfg.valid_end,
            datasets=cfg.datasets,
            freq=cfg.freq,
            algorithm=algo.key,
            fingerprint=fingerprint,
            params_snapshot=params,
            data_versions=await _snapshot_data_versions_for_task(
                market=market, freq=cfg.freq, datasets=cfg.datasets
            ),
        )
        self._tasks[task_id] = task

        from backend.services.engine.alpha_agent.task_store import (
            DuplicateActiveTask,
        )

        # 记录层落行（失败只告警）：历史页第一秒就要能看见这个任务
        try:
            await self._db_create(
                task, source=source, doc_id=doc_id, algorithm_version=algo.version
            )
        except DuplicateActiveTask as dup:
            # 预检之后的竞态窗口（同一进程两路并发提交）：本次撤回，复用赢家
            reuse = self._withdraw_duplicate(task, dup)
            if reuse is not None:
                return reuse.task_id

        self._launch(
            task,
            loop_n=loop_n,
            seed=seed,
            provider_uri=provider_uri,
            direction=direction or "",
            llm_overrides=llm_overrides,
        )
        return task_id

    async def start_or_queue(
        self,
        user_id: str,
        *,
        market: str = "a_share",
        universe: str = "csi300",
        loop_n: int = 5,
        seed: str | None = None,
        direction: str | None = None,
        direction_mode: str | None = None,
        direction_meta: str | None = None,
        quality_gate_mode: str | None = None,
        data_source: str | None = None,
        mining_start: str | None = None,
        mining_end: str | None = None,
        valid_start: str | None = None,
        valid_end: str | None = None,
        datasets: Sequence[str] | None = None,
        freq: str | None = None,
        algorithm: str | None = None,
        source: str = "text",
        doc_id: str | None = None,
        llm_overrides: dict[str, str] | None = None,
        tenant_id: str = "default",
    ) -> QueueReceipt:
        """批量派发入口：有名额立即启动，满则有序排队（最旧优先）。

        与 :meth:`start_evolution`（evolve 端点 429 背压）不同，这里**不拒
        正常提交**——只有排队深度上限（``ALPHA_AGENT_MAX_QUEUED_*``）才抛
        :class:`QueueFullError`，供批量端点逐卡片回可操作错误。

        ``algorithm`` 语义与校验同 :meth:`start_evolution`（同一处 resolve，
        两条入口一种口径）；非法组合同样抛 :class:`AlgorithmError`。

        并发安全的关键：名额判定与占位（PENDING 入内存表）是同一次同步执行，
        中间没有任何 await——并发提交看到的是同一份计数，不会双开超额。

        **同配置去重（P3）**：同用户已有活跃的同指纹任务时回执既有任务
        （``deduplicated=True``，status/位次是它的实况），不新建、不排队、
        不占名额——批量派发里重了卡片不该把队列堵满。
        """
        from backend.services.engine.alpha_agent.hw_lock import (
            assert_factor_mining_hardware,
        )

        algo = resolve_algorithm(
            algorithm, market=market, freq=freq, datasets=list(datasets or [])
        )
        cfg = normalize_mining_config(
            mining_start,
            mining_end,
            valid_start,
            valid_end,
            datasets,
            freq=freq,
            market=market,
        )
        params = _fingerprint_params(
            algo_key=algo.key,
            market=market,
            universe=universe,
            data_source=data_source,
            direction=direction,
            direction_mode=direction_mode,
            quality_gate_mode=quality_gate_mode,
            loop_n=loop_n,
            cfg=cfg,
            source=source,
            doc_id=doc_id,
        )
        fingerprint = fingerprint_of(params)
        existing = self.find_active_duplicate(user_id, fingerprint)
        if existing is not None:
            logger.info(
                "[alpha-agent] 同配置活跃任务复用：user=%s task=%s",
                user_id,
                existing.task_id,
            )
            return QueueReceipt(
                existing.task_id,
                existing.status.value,
                self._queue_position(existing),
                deduplicated=True,
            )

        # 硬件锁按能力面收窄（同 start_evolution）：统计派纯 CPU 不该被 GPU 锁拦
        if algo.requires_gpu:
            assert_factor_mining_hardware()

        counts = self.count_running()
        max_per_user, max_global = self.running_capacity()
        has_slot = (
            counts["by_user"].get(user_id, 0) < max_per_user
            and counts["global"] < max_global
        )
        if not has_slot:
            self._assert_queue_room(user_id)

        task_id = uuid.uuid4().hex[:16]
        task = EvolutionTask(
            task_id=task_id,
            user_id=user_id,
            tenant_id=tenant_id or "default",
            market=market,
            universe=universe,
            loop_n=loop_n,
            data_source=data_source or "",
            direction=direction or "",
            direction_mode=direction_mode or "",
            direction_meta=direction_meta or "",
            quality_gate_mode=quality_gate_mode or "",
            mining_start=cfg.mining_start,
            mining_end=cfg.mining_end,
            valid_start=cfg.valid_start,
            valid_end=cfg.valid_end,
            datasets=cfg.datasets,
            freq=cfg.freq,
            algorithm=algo.key,
            fingerprint=fingerprint,
            params_snapshot=params,
            data_versions=await _snapshot_data_versions_for_task(
                market=market, freq=cfg.freq, datasets=cfg.datasets
            ),
            status=TaskStatus.PENDING if has_slot else TaskStatus.QUEUED,
        )
        self._tasks[task_id] = task

        from backend.services.engine.alpha_agent.task_store import (
            DuplicateActiveTask,
        )

        try:
            await self._db_create(
                task, source=source, doc_id=doc_id, algorithm_version=algo.version
            )
        except DuplicateActiveTask as dup:
            reuse = self._withdraw_duplicate(task, dup)
            if reuse is not None:
                return QueueReceipt(
                    reuse.task_id,
                    reuse.status.value,
                    self._queue_position(reuse),
                    deduplicated=True,
                )

        if task.status == TaskStatus.QUEUED:
            self._owned_queued_task_ids.add(task_id)
            self._persist_task(task)
            return QueueReceipt(task_id, "queued", self._queue_position(task))

        self._launch(
            task,
            loop_n=loop_n,
            seed=seed,
            direction=direction or "",
            llm_overrides=llm_overrides,
        )
        return QueueReceipt(task_id, "running", None)

    async def drain_queue(self) -> int:
        """把排队任务按「最旧优先」补进空闲名额，返回本次启动数。

        触发点：引擎启动 + 每个任务收尾（成功/失败/取消）+ 60s 清扫器。槽位在
        第一个 await **之前**同步预留（QUEUED→PENDING），所以并发 drain
        （``_draining`` 重入闸）与窗口期的新提交（count_running）看到同一份占位。

        **绝不向外抛异常**：单个任务失败（解析不到 LLM 配置 / 启动炸）都落它
        自己的任务行后继续下一个——排水不能拖垮触发它的收尾路径。排空按注册的
        解析器重解析 LLM 配置（队列不持久化密钥）；解析器未注册（主机直跑/
        单测）时 overrides=None，容器 env 兜底。
        """
        if self._draining:
            return 0
        self._draining = True
        started = 0
        try:
            while True:
                counts = self.count_running()
                max_per_user, max_global = self.running_capacity()
                if counts["global"] >= max_global:
                    break
                candidate = next(
                    (
                        t
                        for t in sorted(
                            (
                                t
                                for t in self._tasks.values()
                                if t.status == TaskStatus.QUEUED
                                and t.task_id in self._owned_queued_task_ids
                            ),
                            key=lambda t: t.created_at,
                        )
                        if counts["by_user"].get(t.user_id, 0) < max_per_user
                    ),
                    None,
                )
                if candidate is None:
                    break

                # 同步预留：任何 await 之前先把名额占住
                self._owned_queued_task_ids.discard(candidate.task_id)
                candidate.status = TaskStatus.PENDING
                try:
                    from backend.services.engine.alpha_agent.task_store import (
                        get_mining_task_store,
                    )

                    owned = await get_mining_task_store().claim_queued_task_for_start(
                        candidate.task_id
                    )
                except Exception as e:
                    logger.warning(
                        "queued task lease claim failed for %s: %s",
                        candidate.task_id,
                        e,
                    )
                    candidate.status = TaskStatus.QUEUED
                    self._owned_queued_task_ids.add(candidate.task_id)
                    break
                if owned is False:
                    # Another process claimed or finalized this queue row. Keep the
                    # read-only local copy visible, but never start a duplicate runner.
                    candidate.status = TaskStatus.QUEUED
                    continue

                overrides: dict[str, str] | None = None
                if _llm_override_resolver is not None:
                    overrides = await self._resolve_drain_overrides(candidate)
                    try:
                        candidate_uses_llm = load_algorithm(
                            normalize_algorithm(candidate.algorithm)
                        ).uses_llm
                    except AlgorithmError:
                        candidate_uses_llm = True
                    if overrides is None and candidate_uses_llm:
                        candidate.status = TaskStatus.FAILED
                        candidate.error_message = _QUEUE_LLM_MISSING_MESSAGE
                        self._persist_task(candidate)
                        await self._db_finish(candidate)
                        continue

                try:
                    self._launch(
                        candidate,
                        loop_n=candidate.loop_n,
                        seed=None,
                        direction=candidate.direction,
                        llm_overrides=overrides,
                    )
                except Exception as e:
                    candidate.status = TaskStatus.FAILED
                    candidate.error_message = f"排队任务启动失败: {e}"
                    logger.exception("queued task %s launch failed", candidate.task_id)
                    self._persist_task(candidate)
                    await self._db_finish(candidate)
                    continue
                started += 1
        finally:
            self._draining = False
        return started

    def queued_task_ids(self) -> list[str]:
        """IDs of persisted queue entries visible in this process's task state."""
        return [
            task.task_id
            for task in self._tasks.values()
            if task.status == TaskStatus.QUEUED
        ]

    def adopt_queued_tasks(self, task_ids: Sequence[str]) -> None:
        """Allow queue draining only for leases atomically claimed in PostgreSQL."""
        self._owned_queued_task_ids.update(
            task_id
            for task_id in task_ids
            if task_id in self._tasks
            and self._tasks[task_id].status == TaskStatus.QUEUED
        )

    async def _resolve_drain_overrides(
        self, task: EvolutionTask
    ) -> dict[str, str] | None:
        """排空时的 LLM 覆盖重解析；None = 解析失败（调用方落任务失败）。"""
        assert _llm_override_resolver is not None
        try:
            return await _llm_override_resolver(task.user_id, task.tenant_id)
        except Exception as e:
            logger.warning(
                "queue drain llm re-resolve failed for %s: %s", task.task_id, e
            )
            return None

    async def _drain_after_finish(self) -> None:
        """收尾/取消后的补位排水；排水自身吞异常——绝不改写调用方任务终态。"""
        try:
            await self.drain_queue()
        except Exception as e:
            logger.warning("mining queue drain failed: %s", e)

    def _assert_queue_room(self, user_id: str) -> None:
        q_per_user, q_global = self.queue_capacity()
        queued = [t for t in self._tasks.values() if t.status == TaskStatus.QUEUED]
        user_queued = sum(1 for t in queued if t.user_id == user_id)
        if user_queued >= q_per_user or len(queued) >= q_global:
            raise QueueFullError(
                f"排队已满（您已排队 {user_queued}/{q_per_user}，"
                f"全平台排队 {len(queued)}/{q_global}），"
                "请等待当前任务完成或先取消已排队的任务。"
            )

    def find_active_duplicate(
        self, user_id: str, fingerprint: str
    ) -> EvolutionTask | None:
        """同用户×同指纹的活跃任务（P3 去重的第一道闸；无则 None）。

        只看内存表：本部署 engine 单 worker（``ENGINE_WORKERS=1``——多 worker
        时排队/并发上限本来就已塌），内存表就是「在跑/在排」的唯一事实源。
        DB 里的活跃行可能是上次进程留下的孤儿（启动对账之外还有崩在收尾前
        的窗口），拿它复用等于把一个永远不动的任务回执给用户。
        预检之后的竞态窗口由任务行的部分唯一索引兜底（见 ``_db_create``）。
        """
        if not fingerprint:
            return None
        for task in self._tasks.values():
            if (
                task.user_id == user_id
                and task.fingerprint == fingerprint
                and task.status in _ACTIVE_FOR_DEDUPE
            ):
                return task
        return None

    def _withdraw_duplicate(
        self, task: EvolutionTask, dup: DuplicateActiveTask
    ) -> EvolutionTask | None:
        """撞唯一索引的本次提交 → 撤回内存占位；返回可复用的赢家（无则 None）。

        返回 None 的两条路（调用方都按「没命中去重」继续，fail-open）：
        赢家不在内存（活跃行来自别的进程/重启前的孤儿行——复用它会回执一个
        死任务）；或本次提交在 DB 往返窗口里已被排水启动（已经跑起来了，
        撤回才是真的错：宁可多跑一个也不杀活任务，只记 error）。
        """
        winner = self._tasks.get(dup.existing_task_id)
        if winner is None or winner.status not in _ACTIVE_FOR_DEDUPE:
            logger.warning(
                "[alpha-agent] 去重索引撞行 %s，但内存里没有对应活跃任务"
                "（多为重启前的孤儿行）：本次提交 %s 照常继续，仅记录行缺席",
                dup.existing_task_id,
                task.task_id,
            )
            return None
        if task.process is not None or task.status == TaskStatus.RUNNING:
            logger.error(
                "[alpha-agent] 去重竞态：本次提交 %s 已被排水启动，"
                "与既有任务 %s 同配置并行（不再撤回，避免杀活任务）",
                task.task_id,
                winner.task_id,
            )
            return None
        self._tasks.pop(task.task_id, None)
        return winner

    def _queue_position(self, task: EvolutionTask) -> int | None:
        """本用户队列内位次（1-based，最旧=1）；不在队列里返回 None。"""
        queued = sorted(
            (
                t
                for t in self._tasks.values()
                if t.status == TaskStatus.QUEUED and t.user_id == task.user_id
            ),
            key=lambda t: t.created_at,
        )
        for idx, t in enumerate(queued, start=1):
            if t.task_id == task.task_id:
                return idx
        return None

    def running_capacity(self) -> tuple[int, int]:
        """(每用户上限, 全平台上限)。路由 429 判定与排队判断共用同一读取点。"""
        return (
            _env_int(ENV_MAX_RUNNING_PER_USER, DEFAULT_MAX_RUNNING_PER_USER),
            _env_int(ENV_MAX_RUNNING_GLOBAL, DEFAULT_MAX_RUNNING_GLOBAL),
        )

    def queue_capacity(self) -> tuple[int, int]:
        """(每用户排队深度, 全平台排队深度)。0 = 不许排队（满了直接拒）。"""
        return (
            _env_int(ENV_MAX_QUEUED_PER_USER, DEFAULT_MAX_QUEUED_PER_USER),
            _env_int(ENV_MAX_QUEUED_GLOBAL, DEFAULT_MAX_QUEUED_GLOBAL),
        )

    def _launch(
        self,
        task: EvolutionTask,
        *,
        loop_n: int,
        seed: str | None = None,
        provider_uri: str | None = None,
        direction: str = "",
        llm_overrides: dict[str, str] | None = None,
    ) -> None:
        """解析 provider_uri 并调度 :meth:`_run_evolution`（不等待子进程）。"""
        # Determine provider URI from market adapter if not specified
        if not provider_uri:
            try:
                from backend.services.engine.rd_agent.market_adapters import get_adapter

                adapter = get_adapter(task.market)
                provider_uri = adapter.get_qlib_provider_uri()
            except Exception:
                provider_uri = os.getenv("QLIB_PROVIDER_URI", "/data/qlib/cn_data")

        # Override provider URI based on data_source
        if task.data_source:
            ds = task.data_source.lower().strip()
            if ds == "parquet":
                # 缓存刷新可能遍历 QuantDB 的数千个 9p 分区，不能在
                # FastAPI 事件循环里同步执行；实际准备移到 _run_evolution。
                provider_uri = os.getenv("QLIB_PROVIDER_URI", provider_uri)
            elif ds == "pg":
                provider_uri = "postgresql://localhost:5432/quantmind"
            # qlib_bin uses the default provider_uri

        seed_path = seed or self._default_seed_path()

        asyncio.ensure_future(
            self._run_evolution(
                task,
                loop_n=loop_n,
                seed=seed_path,
                provider_uri=provider_uri,
                direction=direction,
                llm_overrides=llm_overrides,
            )
        )

    async def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        task = self._tasks.get(task_id)
        durable = None
        try:
            durable = await _task_store().get_task(
                task_id, user_id=task.user_id if task else None
            )
        except Exception as exc:
            logger.warning("task state reconciliation failed for %s: %s", task_id, exc)
        if not task:
            return durable
        if durable and durable.get("status") in {"completed", "failed", "cancelled"}:
            await self._apply_terminal_state(task, durable)
        return redact_data({
            "task_id": task.task_id,
            "user_id": task.user_id,
            # 前端全局进度面板据此排序并显示「开始于」；此前这个字段只在磁盘
            # task_state.json 里有，状态接口不返回，前端只能拿不到就填 now()。
            "created_at": _epoch_iso(task.created_at),
            "status": task.status.value,
            "progress": task.progress,
            "phase": task.phase,
            "progress_pct": task.progress_pct,
            "current_loop": task.current_loop,
            "loop_n": task.loop_n,
            # 挖掘算法（移植 P2）：已解析的 manifest key（老任务/未指定 = rdagent）
            "algorithm": normalize_algorithm(task.algorithm),
            # 进度面形状（现读 manifest）：workdir=演化轮（前端渲染 Round x/y），
            # jsonl=单流水线（不分轮——前端不编造演化轮语义）
            "progress_kind": _progress_kind_of(task.algorithm),
            "market": task.market,
            "universe": task.universe,
            "data_source": task.data_source,
            "direction": task.direction,
            "error_message": redact_text(task.error_message) if task.error_message else None,
            "result": task.result,
            "timeline": task.timeline,
            "token_usage": task.token_usage,
            # 自定义挖掘配置快照（移植 P1）：任务详情页据此回显
            # 「这批因子是按哪个区间/哪些数据集挖的」；未指定 → None（默认语义）
            "mining_config": {
                "mining_start": task.mining_start or None,
                "mining_end": task.mining_end or None,
                "valid_start": task.valid_start or None,
                "valid_end": task.valid_end or None,
                "datasets": list(task.datasets) or None,
                # 频率：'' → None（日线默认语义，"没有的值"不伪装成显式 day）
                "freq": task.freq or None,
            },
            # 排队位次（本用户队列内，1-based）；非排队状态恒为 None。
            # 前端「排队中（第 N 位）」直接渲染，不用自己数队列。
            "queue_position": (
                self._queue_position(task) if task.status == TaskStatus.QUEUED else None
            ),
        })

    async def _apply_terminal_state(self, task: EvolutionTask, durable: dict) -> None:
        """A durable terminal state fences both preparation and mining processes."""
        status = TaskStatus(durable["status"])
        changed = task.status != status or task.error_message != durable.get("error")
        if task.status in (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING):
            task._cancel_requested = True
        task.status = status
        task.phase = status.value
        task.error_message = durable.get("error")
        task.progress = {
            TaskStatus.COMPLETED: "因子挖掘完成",
            TaskStatus.FAILED: "任务已停止，请查看失败原因",
            TaskStatus.CANCELLED: "任务已取消",
        }[status]
        task.progress_pct = int(durable.get("progress_pct") or task.progress_pct)
        if status == TaskStatus.COMPLETED:
            task.progress_pct = 100
        if task._cancel_requested and task.process and task.process.poll() is None:
            await _terminate_task_process_group(task.process)
        if changed:
            self._persist_task(task)

    async def reconcile_terminal_tasks(self) -> None:
        for task in list(self._tasks.values()):
            if task.status in (TaskStatus.PENDING, TaskStatus.QUEUED, TaskStatus.RUNNING):
                await self.get_task_status(task.task_id)

    def forget_terminal_task(self, task_id: str) -> bool:
        """Drop a terminal task from this process after its durable row is deleted."""
        task = self._tasks.get(task_id)
        if task is None or task.status in (
            TaskStatus.PENDING,
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
        ):
            return False
        self._tasks.pop(task_id, None)
        self._owned_queued_task_ids.discard(task_id)
        return True

    async def cancel_task(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if not task:
            return False
        if task.status not in (
            TaskStatus.RUNNING,
            TaskStatus.PENDING,
            TaskStatus.QUEUED,
        ):
            return False
        task._cancel_requested = True
        # 排队任务还没有进程，杀掉这一步天然跳过
        if task.process:
            try:
                await _terminate_task_process_group(task.process)
            except ProcessLookupError:
                pass
            except Exception as e:
                logger.warning("Failed to kill process for task %s: %s", task_id, e)
        task.status = TaskStatus.CANCELLED
        task.phase = "cancelled"
        task.progress = "任务已取消"
        task.error_message = "Cancelled by user"
        self._persist_task(task)
        await self._db_cancel(task)
        # 取消运行中的任务腾出名额：排队任务立即补位（排水自身吞异常）
        await self._drain_after_finish()
        return True

    async def get_task_log(self, task_id: str, tail: int = 0) -> str | None:
        """Get subprocess stdout log for a task.

        Args:
            tail: If 0, return the full file. If > 0, return the last N lines.
        """
        task = self._tasks.get(task_id)
        if not task:
            return None
        log_file = self._log_dir / task_id / "subprocess_stdout.log"
        if not log_file.exists():
            return None
        try:
            with open(log_file, errors="replace") as f:
                lines = f.readlines()
            if tail > 0:
                return redact_text("".join(lines[-tail:]))
            return redact_text("".join(lines))
        except Exception:
            return None

    async def list_tasks(self, user_id: str | None = None) -> list[dict[str, Any]]:
        results = []
        for task in list(self._tasks.values()):
            if user_id and task.user_id != user_id:
                continue
            results.append(await self.get_task_status(task.task_id))
        return results

    def count_running(self) -> dict[str, Any]:
        """统计 pending/running 任务数（全局 + 按用户），供并发上限校验。"""
        active = (TaskStatus.PENDING, TaskStatus.RUNNING)
        by_user: dict[str, int] = {}
        total = 0
        for task in self._tasks.values():
            if task.status in active:
                total += 1
                by_user[task.user_id] = by_user.get(task.user_id, 0) + 1
        return {"global": total, "by_user": by_user}

    # ------------------------------------------------------------------
    # 任务记录层（rd_agent_mining_tasks）——任何失败只告警，绝不拦挖掘主链
    # ------------------------------------------------------------------

    async def _db_create(
        self,
        task: EvolutionTask,
        *,
        source: str = "text",
        doc_id: str | None = None,
        algorithm_version: str = "",
    ) -> None:
        """落任务行。普通失败只告警（记录层不拦挖掘主链）；撞去重索引（P3）时
        **上抛** :class:`DuplicateActiveTask`——那不是写失败，是并发竞态下的
        正确拒绝，调用方必须撤回本次提交（见 ``_withdraw_duplicate``）。
        """
        from backend.services.engine.alpha_agent.task_store import (
            DuplicateActiveTask,
        )

        try:
            await _task_store().create_task(
                task_id=task.task_id,
                user_id=task.user_id,
                # 建行状态=建任务时点的状态（pending 立即跑 / queued 已排队）；
                # store 侧只收这两种初始态，running 只能由心跳/对账到来
                status=task.status.value,
                market=task.market,
                universe=task.universe,
                data_source=task.data_source,
                direction=task.direction,
                # 模式未参与（''）→ NULL：历史页不把自由文本伪记成「类别选定」
                direction_mode=task.direction_mode or None,
                # 没抽样（''）→ NULL：抽样证据是事实不是占位，空就不写
                direction_meta=task.direction_meta or None,
                # 未指定（''）→ NULL：任务行存请求意图，不冻结入池时点的 env
                quality_gate_mode=task.quality_gate_mode or None,
                # 自定义挖掘配置（P1）：'' → NULL，数据集存逗号分隔 TEXT
                mining_start=task.mining_start,
                mining_end=task.mining_end,
                valid_start=task.valid_start,
                valid_end=task.valid_end,
                datasets=",".join(task.datasets),
                freq=task.freq,
                algorithm=normalize_algorithm(task.algorithm),
                loop_n=task.loop_n,
                source=source,
                doc_id=doc_id,
                # 去重指纹 + 参数/数据版本快照（P3）：指纹与 params_snapshot 同源
                # （同一份 canonical 字典），事后能从任务行自证为什么没去重
                fingerprint=task.fingerprint,
                algorithm_version=algorithm_version,
                params_snapshot=_json_text(task.params_snapshot),
                data_versions=_json_text(task.data_versions),
            )
        except DuplicateActiveTask:
            raise
        except Exception as e:
            logger.warning("mining task row create failed for %s: %s", task.task_id, e)

    async def _db_sync_progress(self, task: EvolutionTask) -> None:
        try:
            from backend.services.engine.alpha_agent.task_store import (
                TaskLeaseLostError,
            )

            await _task_store().update_progress(
                task.task_id,
                status=task.status.value,
                progress_pct=task.progress_pct,
                current_loop=task.current_loop,
            )
        except TaskLeaseLostError:
            logger.error(
                "mining task lease lost for %s; stopping stale runner", task.task_id
            )
            if not task._cancel_requested:
                task._cancel_requested = True
                task.status = TaskStatus.FAILED
                task.error_message = "Mining task ownership lease was lost"
                task.progress = task.error_message
                task.phase = "failed"
            try:
                durable = await _task_store().get_task(task.task_id, user_id=task.user_id)
                if durable and durable.get("status") in {"completed", "failed", "cancelled"}:
                    await self._apply_terminal_state(task, durable)
            except Exception as exc:
                logger.warning("could not reconcile lost lease for %s: %s", task.task_id, exc)
            self._persist_task(task)
            if task.process:
                try:
                    await _terminate_task_process_group(task.process)
                except Exception as e:
                    logger.warning(
                        "Could not stop runner after lease loss for %s: %s",
                        task.task_id,
                        e,
                    )
        except Exception as e:
            logger.warning(
                "mining task progress sync failed for %s: %s", task.task_id, e
            )

    async def _db_sync_progress_throttled(self, task: EvolutionTask) -> None:
        """内存心跳每 3s 照旧；落到 DB 的进度 ≥_DB_SYNC_INTERVAL_S 一次。"""
        if time.time() - task._last_db_sync < _DB_SYNC_INTERVAL_S:
            return
        task._last_db_sync = time.time()
        await self._db_sync_progress(task)

    async def _db_finish(self, task: EvolutionTask) -> None:
        """终态收尾。取消路径已由 :meth:`_db_cancel` 落 ``cancelled``，此处必须让路。"""
        if task._cancel_requested:
            return
        for attempt in range(_DB_TERMINAL_WRITE_ATTEMPTS):
            try:
                store = _task_store()
                factor_count = await store.count_factors(task.task_id)
                await store.mark_terminal(
                    task.task_id,
                    status=task.status.value,
                    error=redact_text(task.error_message) if task.error_message else None,
                    factor_count=factor_count,
                )
                return
            except Exception as e:
                if attempt + 1 == _DB_TERMINAL_WRITE_ATTEMPTS:
                    logger.error(
                        "mining task terminal write failed after %d attempts for %s: %s",
                        _DB_TERMINAL_WRITE_ATTEMPTS,
                        task.task_id,
                        e,
                    )
                    return
                logger.warning(
                    "mining task terminal write attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    _DB_TERMINAL_WRITE_ATTEMPTS,
                    task.task_id,
                    e,
                )
                await asyncio.sleep(0.25 * (2**attempt))

    def _spawn_factor_naming(self, task: EvolutionTask) -> None:
        """Run optional naming in the API process, outside the secret-stripped runner."""

        async def _name() -> None:
            try:
                from backend.services.engine.alpha_agent.factor_naming import (
                    name_task_factors_cn,
                )

                await name_task_factors_cn(task.task_id, task.user_id)
            except Exception as exc:  # noqa: BLE001 - naming never changes task result
                logger.warning(
                    "factor naming failed for task %s: %s",
                    task.task_id,
                    redact_text(exc),
                )

        background = asyncio.create_task(
            _name(), name=f"factor-naming-{task.task_id}"
        )
        self._background_naming_tasks.add(background)
        background.add_done_callback(self._background_naming_tasks.discard)

    async def _db_cancel(self, task: EvolutionTask) -> None:
        for attempt in range(_DB_TERMINAL_WRITE_ATTEMPTS):
            try:
                await _task_store().mark_terminal(
                    task.task_id, status="cancelled", error="Cancelled by user"
                )
                return
            except Exception as e:
                if attempt + 1 == _DB_TERMINAL_WRITE_ATTEMPTS:
                    logger.error(
                        "mining task cancel write failed after %d attempts for %s: %s",
                        _DB_TERMINAL_WRITE_ATTEMPTS,
                        task.task_id,
                        e,
                    )
                    return
                logger.warning(
                    "mining task cancel write attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    _DB_TERMINAL_WRITE_ATTEMPTS,
                    task.task_id,
                    e,
                )
                await asyncio.sleep(0.25 * (2**attempt))

    def _persist_task(self, task: EvolutionTask) -> None:
        """Save task state to disk so it survives restarts.

        纯尽力而为：排队/取消等提交路径都直接调它，任何失败（含路径计算）
        只告警绝不外抛——持久化失败不能把提交本身打挂。
        """
        try:
            state_file = self._log_dir / task.task_id / "task_state.json"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "task_id": task.task_id,
                "user_id": task.user_id,
                # 租户字段随任务保留以兼容持久化格式；LLM 凭证在排空时只从
                # 当前服务端环境重新解析，密钥本体绝不落盘。
                "tenant_id": task.tenant_id,
                "market": task.market,
                "data_source": task.data_source,
                "universe": task.universe,
                "direction": task.direction,
                # 自定义挖掘配置随盘持久化：重启后幸存的排队任务必须按原窗口/
                # 数据集补位，否则「排了 2 小时、重启后按默认窗口挖」是哑故障
                "mining_start": task.mining_start,
                "mining_end": task.mining_end,
                "valid_start": task.valid_start,
                "valid_end": task.valid_end,
                "datasets": list(task.datasets),
                # 频率随盘：重启后幸存的排队分钟任务必须仍按分钟补位
                "freq": task.freq,
                # 算法随盘：重启后幸存的排队任务必须仍按原算法补位（补位时
                # 从 manifest 现读能力面/runnner——这里存的只是身份 key）
                "algorithm": normalize_algorithm(task.algorithm),
                # 指纹随盘：重启后幸存的活跃任务必须仍参与去重——否则
                # 「重启一次，同配置再点一下」就会双开一份同参挖掘
                "fingerprint": task.fingerprint,
                "status": task.status.value,
                "progress": task.progress,
                "phase": task.phase,
                "progress_pct": task.progress_pct,
                "loop_n": task.loop_n,
                "current_loop": task.current_loop,
                "created_at": task.created_at,
                "error_message": redact_text(task.error_message) if task.error_message else None,
                "timeline": task.timeline,
                "token_usage": task.token_usage,
            }
            process = task.process
            if process is not None and process.poll() is None:
                identity = _linux_process_identity(process.pid)
                data.update(
                    {
                        "owner_pid": os.getpid(),
                        "owner_start_ticks": (
                            (_linux_process_identity(os.getpid()) or (None, None))[1]
                        ),
                        "runner_pid": process.pid,
                        "runner_pgid": identity[0] if identity else None,
                        "runner_start_ticks": identity[1] if identity else None,
                        "boot_id": _linux_boot_id(),
                    }
                )
            else:
                data.update(
                    {
                        "owner_pid": None,
                        "owner_start_ticks": None,
                        "runner_pid": None,
                        "runner_pgid": None,
                        "runner_start_ticks": None,
                        "boot_id": None,
                    }
                )
            with open(state_file, "w") as f:
                json.dump(redact_data(data), f, ensure_ascii=False, default=str)
        except Exception as e:
            logger.warning("Failed to persist task %s: %s", task.task_id, e)

    def _load_tasks(self) -> None:
        """Reload task states from disk on startup."""
        try:
            for task_dir in self._log_dir.iterdir():
                state_file = task_dir / "task_state.json"
                if not state_file.exists():
                    continue
                try:
                    with open(state_file) as f:
                        data = json.load(f)
                    task = EvolutionTask(
                        task_id=data["task_id"],
                        user_id=data["user_id"],
                        tenant_id=data.get("tenant_id", "default") or "default",
                        market=data.get("market", "a_share"),
                        data_source=data.get("data_source", ""),
                        universe=data.get("universe", "csi300"),
                        direction=data.get("direction", ""),
                        mining_start=data.get("mining_start", "") or "",
                        mining_end=data.get("mining_end", "") or "",
                        valid_start=data.get("valid_start", "") or "",
                        valid_end=data.get("valid_end", "") or "",
                        datasets=tuple(data.get("datasets") or ()),
                        freq=data.get("freq", "") or "",
                        # 缺字段 = 迁移前写盘的 state（当时唯一算法）：默认 rdagent
                        algorithm=data.get("algorithm", "") or "",
                        # 缺字段 = 本特性前写盘的 state → ''（不参与去重，
                        # 「未知」不伪装成「无重复」）
                        fingerprint=data.get("fingerprint", "") or "",
                        status=TaskStatus(data.get("status", "pending")),
                        progress=data.get("progress", ""),
                        phase=data.get("phase", "pending"),
                        progress_pct=data.get("progress_pct", 0),
                        loop_n=data.get("loop_n", 3),
                        current_loop=data.get("current_loop", 0),
                        created_at=data.get("created_at", time.time()),
                        error_message=data.get("error_message"),
                        timeline=data.get("timeline", []),
                        token_usage=data.get("token_usage", {}),
                    )
                    if task.status == TaskStatus.RUNNING and _same_process(
                        data.get("owner_pid"),
                        data.get("owner_start_ticks"),
                        data.get("boot_id"),
                    ):
                        # Another live API worker still owns this task. This instance
                        # must neither adopt it nor kill its process group.
                        continue
                    # Running tasks at startup are likely orphaned；
                    # queued 不是孤儿（还没有子进程）——原样保留，启动排水接续
                    if task.status == TaskStatus.RUNNING:
                        _stop_persisted_process_group(data)
                        task.status = TaskStatus.FAILED
                        task.error_message = "Server restarted while task was running"
                    # A runner or backend may have stopped before normal post-run
                    # cleanup. Scrub serialized provider settings for terminal or
                    # queued tasks, but preserve a task still owned by a live worker.
                    removed_settings = _remove_litellm_settings(task_dir)
                    if removed_settings:
                        logger.info(
                            "Removed %d stale RD-Agent LLM settings artifact directories",
                            removed_settings,
                        )
                    self._tasks[task.task_id] = task
                except Exception as e:
                    logger.warning("Failed to load task from %s: %s", state_file, e)
            if self._tasks:
                logger.info("Loaded %d tasks from disk", len(self._tasks))
        except Exception as e:
            logger.warning("Failed to load tasks: %s", e)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _default_seed_path(self) -> str:
        in_container = Path(
            "/app/alphaagent/scenarios/qlib/experiment/factor_data_template"
        )
        if in_container.exists():
            return str(in_container)
        project = os.getenv("HOST_PROJECT_PATH", "/opt/quantmind")
        template = (
            Path(project)
            / "alphaagent"
            / "scenarios"
            / "qlib"
            / "experiment"
            / "factor_data_template"
        )
        return str(template)

    async def _task_heartbeat(self, task: EvolutionTask) -> None:
        """Renew ownership during every await, including slow preparation."""
        while not task._cancel_requested:
            await asyncio.sleep(_DB_SYNC_INTERVAL_S)
            if task.status != TaskStatus.RUNNING or task._cancel_requested:
                return
            await self._db_sync_progress_throttled(task)

    async def _run_evolution(self, task: EvolutionTask, **kwargs: Any) -> None:
        heartbeat = asyncio.create_task(
            self._task_heartbeat(task), name=f"mining-heartbeat-{task.task_id}"
        )
        try:
            await self._execute_evolution(task, **kwargs)
        except asyncio.CancelledError:
            if task.process and task.process.poll() is None:
                await _terminate_task_process_group(task.process)
            raise
        except Exception as exc:
            if not task._cancel_requested:
                task.status = TaskStatus.FAILED
                task.phase = "failed"
                task.error_message = redact_text(exc)
                task.progress = "任务启动失败，请查看失败原因"
                self._persist_task(task)
                await self._db_finish(task)
            logger.exception("Task %s preparation/startup failed", task.task_id)
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            await self._drain_after_finish()

    async def _prepare_qlib_data(self, task: EvolutionTask) -> str | None:
        """Use a process so cancellation also stops DuckDB and cache writes."""
        task_log_dir = self._log_dir / task.task_id
        _secure_task_log_dir(task_log_dir)
        result_file = task_log_dir / "qlib_provider.json"
        result_file.unlink(missing_ok=True)
        log_fd = os.open(
            task_log_dir / "subprocess_stdout.log",
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        log_fh = os.fdopen(log_fd, "ab")
        process = None
        stream_thread = None
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join(
            filter(None, (str(_ALGO_REPO_ROOT), env.get("PYTHONPATH", "")))
        )
        try:
            if task._cancel_requested:
                return None
            process = subprocess.Popen(
                [sys.executable, "-m", "backend.services.engine.alpha_agent.prepare_qlib",
                 "--result-file", str(result_file)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(_ALGO_REPO_ROOT),
                start_new_session=True,
            )
            task.process = process
            assert process.stdout is not None
            stream_thread = threading.Thread(
                target=_stream_redacted_child_output,
                args=(process.stdout, log_fh, env, task.task_id),
                name=f"preparation-log-{task.task_id}",
                daemon=True,
            )
            stream_thread.start()
            self._persist_task(task)
            while process.poll() is None:
                if task._cancel_requested:
                    await _terminate_task_process_group(process)
                    return None
                await asyncio.sleep(0.25)
            await asyncio.to_thread(stream_thread.join, 15)
            if task._cancel_requested:
                return None
            if process.returncode != 0:
                detail = self._tail_error_log(task_log_dir) or f"数据准备进程异常退出（代码 {process.returncode}）"
                raise RuntimeError(
                    f"QuantDB / Qlib 数据准备失败：{detail}"
                )
            return str(json.loads(result_file.read_text(encoding="utf-8"))["provider_uri"])
        finally:
            if process is not None and process.poll() is None:
                await _terminate_task_process_group(process)
            if stream_thread is not None:
                await asyncio.to_thread(stream_thread.join, 15)
            else:
                log_fh.close()
            task.process = None
            result_file.unlink(missing_ok=True)
            self._persist_task(task)

    async def _execute_evolution(
        self,
        task: EvolutionTask,
        *,
        loop_n: int,
        seed: str,
        provider_uri: str,
        direction: str = "",
        llm_overrides: dict[str, str] | None = None,
    ) -> None:
        task.status = TaskStatus.RUNNING
        task.phase = "preparing_data" if task.data_source.lower().strip() == "parquet" else "starting"
        task.progress_pct = 2
        task.progress = "正在准备 QuantDB / Qlib 数据..." if task.phase == "preparing_data" else "正在启动因子挖掘..."
        self._persist_task(task)
        task._last_db_sync = time.time()
        await self._db_sync_progress(task)
        if task._cancel_requested:
            return

        if task.data_source.lower().strip() == "parquet":
            prepared_uri = await self._prepare_qlib_data(task)
            if prepared_uri is None or task._cancel_requested:
                return
            provider_uri = prepared_uri

            task.phase = "starting"
            task.progress = "数据准备完成，正在启动因子挖掘..."
            self._persist_task(task)
            await self._db_sync_progress(task)
            if task._cancel_requested:
                return

        task_log_dir = self._log_dir / task.task_id
        _secure_task_log_dir(task_log_dir)

        # 算法 manifest 现读（任务行只存 key）：runner 路径 / 能力面 / 阶段表
        # 全部来自 manifest。装载失败（被删/改坏）= 任务记录不可执行：显式失败
        # 并给可操作报错，绝不「默默换一个 runner 跑」。
        try:
            algo = load_algorithm(normalize_algorithm(task.algorithm))
        except AlgorithmError as exc:
            task.status = TaskStatus.FAILED
            task.error_message = f"挖掘算法不可用：{exc}"
            task.progress = task.error_message
            self._persist_task(task)
            await self._db_finish(task)
            logger.error("Task %s 算法装载失败: %s", task.task_id, exc)
            return

        # 因子池检索注入（单通道：只写提示词文件，零运行时副作用——绝不碰
        # base_factors.json，否则 LLM 会把历史摘要当可用基础特征送进因子代码
        # 运行时，理由见 pool_service 模块 docstring）。任何失败都空注入，
        # 绝不拦住一次挖矿。只对消费提示词的算法（uses_llm）注入：往统计派
        # 的日志目录写一份没人读的提示词文件，只会污染审计面。
        injection = None
        experience_path = None
        if algo.uses_llm:
            from backend.services.engine.mining_plugins import pool_service

            injection = await pool_service.prepare_injection(
                user_id=task.user_id,
                market=task.market,
                universe=task.universe,
                task_id=task.task_id,
                log_dir=task_log_dir,
            )

            # T-FM-04 经验记忆/禁区注入：与池注入同一单通道纪律（只写提示词
            # 文件，零运行时副作用）。纯派生层、任何失败返回 None 且只告警，
            # 绝不拦住一次挖矿；落盘文件本身即注入内容的审计凭证。
            from backend.services.engine.alpha_agent import experience_memory

            experience_path = await experience_memory.prepare_injection(
                user_id=task.user_id,
                market=task.market,
                task_id=task.task_id,
                log_dir=task_log_dir,
            )

        # Build environment
        env = {
            **os.environ,
            "PYTHONPATH": os.getenv("PYTHONPATH") or "/app",
            "LOG_TRACE_PATH": str(task_log_dir),
            "QLIB_PROVIDER_URI": provider_uri,
            "QLIB_FACTOR_UNIVERSE": task.universe,
            # 回测数据从 2016 年开始 (默认 2008 太慢)
            "QLIB_FACTOR_TRAIN_START": os.getenv(
                "QLIB_FACTOR_TRAIN_START", "2016-01-01"
            ),
            "QLIB_FACTOR_VALID_START": os.getenv(
                "QLIB_FACTOR_VALID_START", "2021-01-01"
            ),
            "QLIB_FACTOR_VALID_END": os.getenv("QLIB_FACTOR_VALID_END", "2022-12-31"),
            "QLIB_FACTOR_TEST_START": os.getenv("QLIB_FACTOR_TEST_START", "2023-01-01"),
            "QLIB_FACTOR_TEST_END": os.getenv("QLIB_FACTOR_TEST_END", "2025-12-31"),
            # 因子处理并行数
            "MULTI_PROC_N": os.getenv("MULTI_PROC_N", "4"),
        }

        # LLM 链路只对消费提示词的算法（uses_llm）铺设：服务端环境解析出的
        # 供应商变量、CoSTEER 知识库、向量检索配置和提示词池上下文。统计算法
        # 不读这些变量，因此不向它的子进程注入部署级凭证。
        if algo.uses_llm:
            openai_base = (
                os.getenv("OPENAI_BASE_URL") or os.getenv("OPENAI_API_BASE") or ""
            )
            openai_api_key = (
                os.getenv("AI_IDE_LLM_API_KEY")
                or os.getenv("AI_IDE_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or ""
            )
            chat_model = os.getenv("CHAT_MODEL", "")
            system_prompt = os.getenv("ALPHA_AGENT_SYSTEM_PROMPT", "")

            # llm_overrides 由路由从服务端环境构造；仅覆盖当前子进程环境，
            # 不接受请求体/用户 Profile 中的凭证。
            if llm_overrides:
                openai_base = llm_overrides.get("OPENAI_BASE_URL", openai_base)
                openai_api_key = llm_overrides.get("OPENAI_API_KEY", openai_api_key)
                chat_model = llm_overrides.get("CHAT_MODEL", chat_model)

            # CoSTEER 经验记忆（知识库）：跨任务累积「因子代码怎么写才过评测」的经验。
            # 读写路径、绝对路径约束与 filelock 的理由见 rd_agent/kb_env.py。
            from backend.services.engine.rd_agent.kb_env import knowledge_base_env

            env.update(knowledge_base_env())
            env["REASONING_MODEL"] = chat_model
            env["CHAT_STREAM"] = "false"
            if system_prompt:
                env["DEFAULT_SYSTEM_PROMPT"] = system_prompt
            if openai_base:
                env["OPENAI_BASE_URL"] = openai_base
            if openai_api_key:
                env["OPENAI_API_KEY"] = openai_api_key
            if chat_model:
                env["CHAT_MODEL"] = chat_model
            if injection is not None and injection.path is not None:
                # rd_loop_wrapper._build_prompt_suffix 读这个文件追加「历史挖掘记忆」段
                env["QMF_POOL_CONTEXT_PATH"] = str(injection.path)
            if experience_path is not None:
                # rd_loop_wrapper._experience_context_block 读这个文件追加「经验记忆」段
                env["QMF_EXPERIENCE_CONTEXT_PATH"] = str(experience_path)

            # 补齐 RD-Agent litellm 后端需要的 LITELLM_ 前缀变量（deepseek 优先）
            if llm_overrides:
                # 显式写入 LITELLM_ 覆盖，绕开 build_llm_env 里 os.getenv 的 mock 占位符链
                for _k in ("LITELLM_OPENAI_API_KEY", "LITELLM_OPENAI_API_BASE", "LITELLM_CHAT_MODEL"):
                    if llm_overrides.get(_k):
                        env[_k] = llm_overrides[_k]

            from backend.services.engine.rd_agent.llm_env import (
                build_llm_env,
                embedding_overrides,
            )

            # 服务端向量检索配置与 chat 是**独立通道**，必须单独透传；
            # 键清单与理由见 llm_env.embedding_overrides。
            env.update(embedding_overrides(llm_overrides))

            build_llm_env(env)
        elif llm_overrides:
            # 统计挖掘仍是纯统计；这些覆盖只供 runner 收尾时的可选中文命名，
            # 不进入表达式生成、数据计算或回测逻辑。
            for key in (
                "OPENAI_API_KEY",
                "OPENAI_BASE_URL",
                "CHAT_MODEL",
                "LLM_EXTRA_HEADERS",
            ):
                if llm_overrides.get(key):
                    env[key] = llm_overrides[key]

        if llm_overrides:
            # 中文命名服务用独立变量保留当前服务端 LLM 配置；密钥仅在本次
            # 任务子进程环境中使用，不落任务行。
            for source, target in (
                ("OPENAI_API_KEY", "QMF_FACTOR_NAME_API_KEY"),
                ("OPENAI_BASE_URL", "QMF_FACTOR_NAME_BASE_URL"),
                ("CHAT_MODEL", "QMF_FACTOR_NAME_MODEL"),
                ("LLM_EXTRA_HEADERS", "QMF_FACTOR_NAME_HEADERS"),
            ):
                if llm_overrides.get(source):
                    env[target] = llm_overrides[source]

        # Add market adapter env overrides (RD-Agent runner used for all markets)
        try:
            from backend.services.engine.rd_agent.market_adapters import get_adapter

            adapter = get_adapter(task.market)
            adapter_env = adapter.get_env_overrides()
            env.update(adapter_env)
            # Adapters may emit empty CHAT_MODEL from the API process. Restore
            # the already selected LITELLM model before spawning the runner.
            if algo.uses_llm:
                build_llm_env(env)
        except Exception as e:
            logger.warning("Failed to get market adapter env: %s", e)

        # 自定义挖掘窗口/数据集（移植 P1）：以**任务参数**为准注入
        # 子进程 env（QM_* 主契约 + 旧 QLIB_FACTOR_* 同源），排在适配器 env 之后
        # ——任务选择必须压过市场默认。参数解析失败 = 任务记录不可信：fail 任务，
        # 绝不静默退回默认窗口去挖一批错区间的因子。
        if task.mining_start or task.valid_start:
            try:
                # 覆盖校验不在这里做：任务行是请求入口按当时覆盖校验过的
                # 事实，重启补位时数据可能又长了几天，拿新覆盖反咬旧任务没有意义。
                # 末端保护（data_end）要现算现用：数据每天都在长，服务器重启后
                # 补位执行的老任务同样不能把回测终点压在最后一根 bar 上。
                from backend.services.engine.rd_agent.rd_loop_wrapper import (
                    qlib_backtest_data_end,
                )

                data_end = qlib_backtest_data_end()
                mining, validation = resolve_windows(
                    task.mining_start,
                    task.mining_end,
                    task.valid_start,
                    task.valid_end,
                    data_end=data_end,
                )
                if mining is not None:
                    # 分钟任务不落数据集（空元组 → task_env 直接不发该键）：把空元组
                    # 补成"全选"会让子进程以为用户选了一堆日线富化列并记 warning。
                    # 不消费数据集的算法（统计派）同理：不发该键，而不是发一份
                    # 没人读的"全选"——env 里出现的每个键都该有人消费。
                    if not algo.uses_datasets:
                        selected: tuple[str, ...] = ()
                    elif task.freq:
                        selected = task.datasets
                    else:
                        selected = task.datasets or SUPPORTED_DATASETS
                    env.update(
                        task_env(mining, validation, selected, data_end=data_end)
                    )
            except ValueError as exc:
                task.status = TaskStatus.FAILED
                task.error_message = f"挖掘配置非法：{exc}"
                task.progress = task.error_message
                self._persist_task(task)
                await self._db_finish(task)
                logger.error("Task %s 挖掘配置非法，拒绝启动: %s", task.task_id, exc)
                return

        # 挖掘频率：与窗口**独立**注入。分钟任务缺窗口时由 wrapper 响亮失败
        # （MinuteDataError），但频率标记必须先到——否则子进程按日线默认跑完，
        # 产出一批"自称分钟因子"的东西，正是 P1.5 要消灭的失败态。
        if task.freq:
            env[ENV_FREQ] = task.freq

        # P3 任务指纹：统计派落库的稳定因子 id 靠它定身份（同配置重跑幂等刷新
        # 同一行）。空指纹不注入——runner 会如实退回任务级身份，而不是拿空串
        # 把所有任务的身份撞成一个。
        if task.fingerprint:
            env[ENV_TASK_FINGERPRINT] = task.fingerprint

        _strip_unneeded_child_secrets(
            env,
            needs_llm=algo.uses_llm,
            needs_factor_name_llm=bool(llm_overrides),
            needs_database=algo.key == "statistical",
        )

        cmd = _build_runner_cmd(
            task, algo, loop_n=loop_n, direction=direction, log_dir=task_log_dir
        )

        logger.info(
            "Starting factor mining: market=%s, algorithm=%s, script=%s",
            task.market,
            algo.key,
            algo.runner,
        )
        logger.info("Command: %s", " ".join(cmd))

        stdout_log = task_log_dir / "subprocess_stdout.log"
        log_fh = None
        stream_thread: threading.Thread | None = None

        try:
            # Preparation (Qlib cache, pool retrieval, config resolution) can take
            # long enough for another process to claim or finalize this task.
            # Revalidate immediately before spawning untrusted runner code.
            await self._db_sync_progress(task)
            if task._cancel_requested:
                return

            log_flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
            log_fd = os.open(stdout_log, log_flags, 0o600)
            if os.name == "posix":
                os.fchmod(log_fd, 0o600)
            log_fh = os.fdopen(log_fd, "ab")
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                cwd=str(task_log_dir),
                # start_new_session 等价于 setsid（进程组隔离，取消时可 killpg），
                # 但比 preexec_fn=os.setsid 安全（preexec_fn 在多线程 asyncio 环境会段错误 -11）
                start_new_session=True,
            )
            assert process.stdout is not None
            stream_thread = threading.Thread(
                target=_stream_redacted_child_output,
                args=(process.stdout, log_fh, env, task.task_id),
                name=f"task-log-redactor-{task.task_id}",
                daemon=True,
            )
            stream_thread.start()
            task.process = process
            self._persist_task(task)

            # spawn 成功才记检索疲劳（提示词已随子进程启动送达）；
            # mark_retrieved 自身吞异常，失败最多少计一次，绝不影响挖掘
            if algo.uses_llm and injection is not None and injection.factor_ids:
                await pool_service.mark_retrieved(injection.factor_ids)

            while process.poll() is None:
                if task._cancel_requested:
                    try:
                        pgid = os.getpgid(process.pid)
                        os.killpg(pgid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                    break
                self._refresh_progress(task, task_log_dir, algo)
                if task._cancel_requested:
                    break
                await asyncio.sleep(3)

            if process.poll() is None:
                process.wait(timeout=30)

            if stream_thread is not None:
                await asyncio.to_thread(stream_thread.join, 15)
                if stream_thread.is_alive():
                    logger.warning(
                        "subprocess log stream still open after task %s exited",
                        task.task_id,
                    )

            # RD-Agent writes common_logs.log directly instead of stdout; scrub its
            # on-disk copy once the runner has stopped writing to the task directory.
            await asyncio.to_thread(_sanitize_common_logs, task_log_dir, env)

            # RD-Agent also serializes LiteLLM config (including env-provided API
            # keys) below LITELLM_SETTINGS. These snapshots are not result data.
            removed_settings = await asyncio.to_thread(
                _remove_litellm_settings, task_log_dir
            )
            if removed_settings:
                logger.info(
                    "Removed %d RD-Agent LLM settings artifact directories for task %s",
                    removed_settings,
                    task.task_id,
                )

            if task._cancel_requested:
                return

            # 收尾前最后一次刷新：把子进程写出的末几个阶段收进时间线
            self._refresh_progress(task, task_log_dir, algo, finished=True)

            if process.returncode == 0:
                task.result = await self._persist_rd_agent_results(task, task_log_dir)
                if task._cancel_requested:
                    return
                task.status = TaskStatus.COMPLETED
                task.phase = "completed"
                task.progress_pct = 100
                task.progress = "因子挖掘完成"
                self._spawn_factor_naming(task)
            else:
                task.status = TaskStatus.FAILED
                task.phase = "failed"
                error_output = self._tail_error_log(task_log_dir)
                task.error_message = (
                    f"Process exited with code {process.returncode}: {error_output}"
                )
                logger.error(
                    "Factor mining failed for task %s: %s",
                    task.task_id,
                    task.error_message,
                )

            self._persist_task(task)
            await self._db_finish(task)

        except Exception as e:
            if task._cancel_requested:
                return
            task.status = TaskStatus.FAILED
            task.phase = "failed"
            task.error_message = redact_text(e)
            logger.exception("Factor mining exception for task %s", task.task_id)
            if stream_thread is None and log_fh is not None:
                try:
                    log_fh.close()
                except Exception:
                    pass
            self._persist_task(task)
            await self._db_finish(task)

    _PHASE_ORDER = [
        ("scenario", "scenario", "初始化场景"),
        ("hypothesis generation", "hypothesis", "生成假设"),
        ("hypothesis generator", "hypothesis", "生成假设"),
        ("experiment generation", "experiment", "设计实验"),
        ("evolving code", "coder", "进化编写代码"),
        ("coder", "coder", "编写因子代码"),
        ("coding", "coder", "编写因子代码"),
        ("runner", "runner", "回测运行因子"),
        ("summarizer", "summarizer", "总结结果"),
    ]

    @staticmethod
    def _find_active_phase(root: Path) -> tuple[str, str, str] | None:
        best: tuple[float, tuple[str, str, str]] = (-1.0, ("", "", ""))
        # Search paths: direct children + direct_exp_gen/ subdirectory
        search_roots = [root]
        deg = root / "direct_exp_gen"
        if deg.is_dir():
            search_roots.append(deg)
        for search_root in search_roots:
            for sub_name, key, label in AlphaAgentLauncher._PHASE_ORDER:
                sub = search_root / sub_name
                if not sub.is_dir():
                    continue
                newest = -1.0
                try:
                    for f in sub.rglob("*"):
                        if f.is_file():
                            try:
                                mt = f.stat().st_mtime
                            except OSError:
                                continue
                            if mt > newest:
                                newest = mt
                except OSError:
                    continue
                if newest > best[0]:
                    best = (newest, (sub_name, key, label))
        return best[1] if best[0] > 0 else None

    #: 单流水线 runner（统计派等 manifest 声明的算法）的进度文件：每行一个
    #: JSON 事件 ``{stage, detail, ts}``，阶段 key 由 manifest ``stages`` 翻译。
    _JSONL_PROGRESS_FILE = "progress.jsonl"

    def _refresh_progress(
        self,
        task: EvolutionTask,
        log_dir: Path,
        algo: MiningAlgorithm,
        *,
        finished: bool = False,
    ) -> None:
        """按 manifest ``progress_kind`` 分派进度读取器（契约显式声明，不嗅探）。"""
        if algo.progress_kind == PROGRESS_KIND_JSONL:
            self._update_jsonl_progress(task, log_dir, algo, finished=finished)
        else:
            self._update_progress(task, log_dir)

    @staticmethod
    def _jsonl_events(path: Path) -> list[dict[str, Any]]:
        """读 progress.jsonl；坏行/半截行跳过（**正在写入**的末行不是错误）。"""
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        events: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def _update_jsonl_progress(
        self,
        task: EvolutionTask,
        log_dir: Path,
        algo: MiningAlgorithm,
        *,
        finished: bool = False,
    ) -> None:
        """``progress.jsonl`` → 阶段/百分比/时间线（无 loop 概念的单流水线算法）。

        百分比按 manifest stages 的序位线性推进并**封顶 99**——100 只由收尾
        路径置（完成度的终点是落库收尾，不是最后一个业务阶段）。时间线沿用
        RD-Agent 的同一外形（loop → phases），前端一套渲染器吃两种算法。
        manifest 声明的**全部**阶段一开始就摆进时间线（未到的 = pending，
        用户一眼看到完整流水线，前端分母不再靠猜）；未在 manifest 声明的
        阶段如实追加（不硬编码算法名/阶段名）。无任何事件时不动任务状态
        （runner 还没开口，不产假时间线）。
        """
        events = self._jsonl_events(log_dir / self._JSONL_PROGRESS_FILE)
        if not events:
            return
        labels = {s.key: s.label for s in algo.stages}
        order = {s.key: i for i, s in enumerate(algo.stages)}

        def _pending(key: str) -> dict[str, Any]:
            return {
                "key": key,
                "label": labels.get(key, key),
                "status": "pending",
                "start_time": None,
                "end_time": None,
                "detail": "",
                "duration_s": None,
            }

        nodes: dict[str, dict[str, Any]] = {}
        seen: list[str] = []  # 出现顺序（状态收口与 pct 只看它，pending 不参与）
        extra: list[str] = []  # 未在 manifest 声明的 stage：如实追加在最后
        #: stage → [start_epoch, end_epoch]，只由**真实事件时刻**推 duration——
        #: 不借「读文件时刻」凑数（这里存的是过程量，不进前端字典）
        spans: dict[str, list[float | None]] = {}

        def _close(prev_key: str, ts: float | None, iso: str | None) -> None:
            """给上一阶段补 end_time（只在还没有结束证据时）。"""
            node = nodes[prev_key]
            if node["end_time"] is not None or iso is None:
                return
            node["end_time"] = iso
            spans[prev_key][1] = ts

        for event in events:
            stage = str(event.get("stage") or "").strip()
            if not stage:
                continue
            raw_ts = event.get("ts")
            ts = float(raw_ts) if isinstance(raw_ts, (int, float)) else None
            iso = _epoch_iso(raw_ts)
            node = nodes.get(stage)
            if node is None:
                # 新阶段开始 = 上一阶段结束：单流水线顺序执行，下一个阶段的
                # 首个事件就是前一阶段结束的唯一真实证据（不伪造收尾时刻）。
                if seen:
                    _close(seen[-1], ts, iso)
                node = {
                    "key": stage,
                    "label": labels.get(stage, stage),
                    "status": "running",
                    "start_time": iso,
                    "end_time": None,
                    "detail": "",
                }
                nodes[stage] = node
                spans[stage] = [ts, None]
                seen.append(stage)
                if stage not in order:
                    extra.append(stage)
            elif iso:
                # 同阶段多次上报（如 evaluate 逐个候选）：末次事件即阶段终点
                node["end_time"] = iso
                spans[stage][1] = ts
            detail = str(event.get("detail") or "").strip()
            if detail:
                node["detail"] = detail

        # 阶段收口：最后出现的阶段在跑（finished → 已完成），更早的已完成
        for key in seen[:-1]:
            nodes[key]["status"] = "completed"
        if finished:
            nodes[seen[-1]]["status"] = "completed"

        # 组装：manifest 声明序打底 + 未声明阶段按出现序追加
        phases = [nodes.get(s.key) or _pending(s.key) for s in algo.stages]
        phases += [nodes[key] for key in extra]

        # duration 与 RD-Agent 时间线同形（前端一套渲染器吃两种算法）
        for node in phases:
            start_ts, end_ts = spans.get(node["key"]) or (None, None)
            node["duration_s"] = (
                round(end_ts - start_ts, 1)
                if start_ts is not None and end_ts is not None
                else None
            )

        last_stage = seen[-1]
        stage_idx = order.get(last_stage)
        if stage_idx is not None:
            frac = (stage_idx + 1) / len(algo.stages)
            task.progress_pct = max(task.progress_pct, min(99, int(frac * 100)))
        task.phase = last_stage
        detail = nodes[last_stage]["detail"]
        label = labels.get(last_stage, last_stage)
        task.progress = f"{label} — {detail}" if detail else label
        task.timeline = [
            {
                "loop": 0,
                "label": algo.name,
                "status": "completed" if finished else "running",
                "phases": phases,
            }
        ]

    def _update_progress(self, task: EvolutionTask, log_dir: Path) -> None:
        r_dir = log_dir / "r"
        d_dir = log_dir / "d"
        loop_dirs = sorted(
            [p for p in log_dir.glob("Loop_*") if p.is_dir()],
            key=lambda p: (
                int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else 0
            ),
        )

        candidates: list[tuple[int, Path]] = []
        if r_dir.is_dir():
            candidates.append((0, r_dir))
        if d_dir.is_dir():
            candidates.append((1, d_dir))
        for ld in loop_dirs:
            try:
                idx = int(ld.name.split("_", 1)[1])
            except (IndexError, ValueError):
                continue
            candidates.append((idx, ld))

        if not candidates:
            task.phase = "starting"
            task.progress_pct = 2
            task.progress = "正在启动因子挖掘..."
            return

        best_candidate = candidates[0]
        best_mtime = -1.0
        for loop_idx, cpath in candidates:
            newest = -1.0
            try:
                for f in cpath.rglob("*"):
                    if f.is_file():
                        try:
                            mt = f.stat().st_mtime
                        except OSError:
                            continue
                        if mt > newest:
                            newest = mt
            except OSError:
                continue
            if newest > best_mtime:
                best_mtime = newest
                best_candidate = (loop_idx, cpath)

        task.current_loop = best_candidate[0]
        phase_root = best_candidate[1]

        result = self._find_active_phase(phase_root)
        if result is None:
            task.phase = "starting"
            task.progress_pct = 5
            task.progress = (
                f"Loop {task.current_loop}/{task.loop_n} — 准备中..."
                if task.current_loop
                else "首轮启动中..."
            )
            return

        sub_name, key, label = result
        task.phase = key
        try:
            phase_idx = [p[0] for p in self._PHASE_ORDER].index(sub_name)
        except ValueError:
            phase_idx = 0
        phase_frac = (phase_idx + 1) / len(self._PHASE_ORDER)

        total_units = max(task.loop_n + 1, 1)
        loop_frac = (task.current_loop + phase_frac) / total_units
        task.progress_pct = max(5, min(99, int(loop_frac * 100)))

        loop_tag = (
            "首轮"
            if task.current_loop == 0
            else f"Loop {task.current_loop}/{task.loop_n}"
        )
        task.progress = f"{loop_tag} — {label}"

        # 构建详细时间线
        task.timeline = self._build_timeline(log_dir, task.loop_n)
        task.token_usage = self._aggregate_token_usage(log_dir)

    _PHASE_DIR_MAP = {
        "hypothesis generation": ("hypothesis", "生成假设"),
        "experiment generation": ("experiment", "设计实验"),
        "coder": ("coder", "编写因子代码"),
        "coding": ("coder", "编写因子代码"),
        "runner": ("runner", "回测运行"),
        "running": ("runner", "回测运行"),
        "feedback": ("feedback", "总结反馈"),
    }

    @staticmethod
    def _load_pkl(path: Path) -> Any:
        """pkl 读取（受限反序列化，机制见 ``shared.safe_pickle``）；失败一律 None。"""
        try:
            return load_pickle(path)
        except Exception:
            return None

    def _build_timeline(self, log_dir: Path, loop_n: int) -> list[dict[str, Any]]:
        """从日志目录构建详细时间线"""
        timeline: list[dict[str, Any]] = []

        loop_dirs = sorted(
            [p for p in log_dir.glob("Loop_*") if p.is_dir()],
            key=lambda p: (
                int(p.name.split("_", 1)[1]) if p.name.split("_", 1)[1].isdigit() else 0
            ),
        )

        for loop_dir in loop_dirs:
            try:
                loop_idx = int(loop_dir.name.split("_", 1)[1])
            except (IndexError, ValueError):
                continue

            loop_entry: dict[str, Any] = {
                "loop": loop_idx,
                "label": "首轮" if loop_idx == 0 else f"Loop {loop_idx}/{loop_n}",
                "phases": [],
                "status": "running",
            }

            # direct_exp_gen 下有 hypothesis generation + experiment generation
            deg = loop_dir / "direct_exp_gen"
            if deg.is_dir():
                for phase_dir_name in (
                    "hypothesis generation",
                    "experiment generation",
                ):
                    phase_dir = deg / phase_dir_name
                    if phase_dir.is_dir():
                        phase_info = self._extract_phase_info(phase_dir, phase_dir_name)
                        if phase_info:
                            loop_entry["phases"].append(phase_info)

            # coding
            coding_dir = loop_dir / "coding"
            if coding_dir.is_dir():
                phase_info = self._extract_phase_info(coding_dir, "coding")
                if phase_info:
                    # 提取编码中的因子名
                    phase_info["factors"] = self._extract_coding_factors(coding_dir)
                    loop_entry["phases"].append(phase_info)

            # running
            running_dir = loop_dir / "running"
            if running_dir.is_dir():
                phase_info = self._extract_phase_info(running_dir, "running")
                if phase_info:
                    loop_entry["phases"].append(phase_info)

            # feedback
            feedback_dir = loop_dir / "feedback"
            if feedback_dir.is_dir():
                phase_info = self._extract_phase_info(feedback_dir, "feedback")
                if phase_info:
                    loop_entry["phases"].append(phase_info)

            # 判断 loop 状态
            has_feedback = any(
                p.get("key") == "feedback" and p.get("status") == "completed"
                for p in loop_entry["phases"]
            )
            has_runner = any(p.get("key") == "runner" for p in loop_entry["phases"])
            if has_feedback:
                loop_entry["status"] = "completed"
            elif has_runner:
                loop_entry["status"] = "backtesting"
            else:
                loop_entry["status"] = "running"

            timeline.append(loop_entry)

        return timeline

    def _extract_phase_info(
        self, phase_dir: Path, dir_name: str
    ) -> dict[str, Any] | None:
        """从阶段目录提取时间信息"""
        key, label = self._PHASE_DIR_MAP.get(dir_name, (dir_name, dir_name))

        # 找 time_info pickle
        time_info_files = list(phase_dir.glob("**/time_info/**/*.pkl"))
        start_time = None
        end_time = None
        duration_s = None

        if time_info_files:
            data = self._load_pkl(time_info_files[0])
            if isinstance(data, dict):
                start_time = data.get("start_time")
                end_time = data.get("end_time")
                if start_time and end_time:
                    duration_s = (end_time - start_time).total_seconds()

        # 如果没有 time_info, 用文件 mtime 推断
        if not start_time:
            try:
                files = sorted(phase_dir.rglob("*"), key=lambda f: f.stat().st_mtime)
                if files:
                    start_time = datetime.fromtimestamp(
                        files[0].stat().st_mtime, tz=timezone.utc
                    )
                    end_time = datetime.fromtimestamp(
                        files[-1].stat().st_mtime, tz=timezone.utc
                    )
                    duration_s = (end_time - start_time).total_seconds()
            except Exception:
                pass

        # 找 token_cost
        token_files = list(phase_dir.glob("**/token_cost/**/*.pkl"))
        tokens = {"prompt": 0, "completion": 0, "calls": 0}
        for tf in token_files:
            data = self._load_pkl(tf)
            if isinstance(data, dict):
                tokens["prompt"] += data.get("prompt_tokens", 0) or 0
                tokens["completion"] += data.get("completion_tokens", 0) or 0
                tokens["calls"] += 1

        # 判断状态
        status = "completed" if end_time else ("running" if start_time else "pending")

        entry: dict[str, Any] = {
            "key": key,
            "label": label,
            "status": status,
            "start_time": start_time.isoformat() if start_time else None,
            "end_time": end_time.isoformat() if end_time else None,
            "duration_s": round(duration_s, 1) if duration_s else None,
        }
        if tokens["calls"] > 0:
            entry["tokens"] = tokens
        return entry

    def _extract_coding_factors(self, coding_dir: Path) -> list[str]:
        """从 coding 目录提取正在编码的因子名"""
        factors: list[str] = []
        # 实验生成 pkl 里有因子名
        for pkl_path in coding_dir.glob("**/experiment generation/**/*.pkl"):
            data = self._load_pkl(pkl_path)
            if isinstance(data, list):
                for t in data:
                    name = getattr(t, "factor_name", None) or getattr(t, "name", None)
                    if name:
                        factors.append(name)
            elif data is not None:
                name = getattr(data, "factor_name", None) or getattr(data, "name", None)
                if name:
                    factors.append(name)
        return factors

    @staticmethod
    def _aggregate_token_usage(log_dir: Path) -> dict[str, Any]:
        """汇总所有 LLM token 用量"""
        total_prompt = 0
        total_completion = 0
        total_calls = 0
        models: set[str] = set()

        for pkl_path in log_dir.glob("**/token_cost/**/*.pkl"):
            try:
                data = load_pickle(pkl_path)
                if isinstance(data, dict):
                    total_prompt += data.get("prompt_tokens", 0) or 0
                    total_completion += data.get("completion_tokens", 0) or 0
                    total_calls += 1
                    if data.get("model"):
                        models.add(data["model"])
            except Exception:
                continue

        return {
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_calls": total_calls,
            "models": list(models),
        }

    async def _persist_rd_agent_results(
        self, task: EvolutionTask, log_dir: Path
    ) -> dict[str, Any]:
        """Persist RD-Agent output after its secret-stripped runner has exited."""
        result_file = log_dir / "result.json"
        result = self._collect_results(task, log_dir)
        if not result.get("persistence_pending"):
            return result

        factors = result.get("factors")
        if not isinstance(factors, list) or len(factors) > 100:
            raise RuntimeError("RD-Agent result has an invalid factor list")
        if any(not isinstance(item, dict) for item in factors):
            raise RuntimeError("RD-Agent result contains an invalid factor record")

        import hashlib
        from types import SimpleNamespace

        from backend.services.engine.qlib_app.services.rd_agent_persistence import (
            RDAgentFactorPersistence,
        )

        persistence = RDAgentFactorPersistence()
        await persistence.ensure_tables()
        corpus_limit = 5000
        corpus = await persistence.list_factors(
            market=task.market, limit=corpus_limit
        )
        if len(corpus) >= corpus_limit:
            logger.warning(
                "RD-Agent task %s dedupe corpus reached %d rows; older factors were not checked",
                task.task_id,
                corpus_limit,
            )
        from scripts.alpha_agent.run_rd_agent import (
            _dedupe_against_corpus,
            _maybe_spawn_materialize,
            _maybe_spawn_validate,
        )

        duplicate_ids = _dedupe_against_corpus(corpus, factors, task.task_id)
        saved = 0
        for factor in factors:
            name = str(factor.get("name") or "").strip()
            code = factor.get("code")
            if (
                not name
                or len(name) > 500
                or not isinstance(code, str)
                or len(code.encode("utf-8")) > 100_000
            ):
                raise RuntimeError("RD-Agent result contains an invalid factor name/code")
            factor_id = hashlib.md5(
                f"{task.task_id}:{name}".encode("utf-8")
            ).hexdigest()
            if factor_id in duplicate_ids:
                continue
            metadata = factor.get("metadata")
            if not isinstance(metadata, dict):
                raise RuntimeError(f"RD-Agent factor {name!r} has invalid metadata")
            if len(json.dumps(metadata, ensure_ascii=False, allow_nan=False)) > 100_000:
                raise RuntimeError(f"RD-Agent factor {name!r} metadata is too large")
            metadata = {
                **metadata,
                "source": "rd_agent",
                "market": task.market,
                "task_id": task.task_id,
            }
            status = factor.get("status") or "pending"
            if status not in {"pending", "completed"}:
                raise RuntimeError(f"RD-Agent factor {name!r} has invalid status")
            metrics: dict[str, float | None] = {}
            for key in ("ic_value", "rank_ic"):
                value = factor.get(key)
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        raise RuntimeError(f"RD-Agent factor {name!r} has invalid {key}")
                    if not math.isfinite(float(value)):
                        raise RuntimeError(f"RD-Agent factor {name!r} has non-finite {key}")
                    metrics[key] = float(value)
                else:
                    metrics[key] = None
            await persistence.save_factor(
                factor_id=factor_id,
                factor_name=name,
                factor_code=code,
                user_id=task.user_id,
                metadata=metadata,
                market=task.market,
                universe=task.universe,
                factor_formulation=str(factor.get("formulation") or ""),
            )
            await persistence.update_factor_metrics(
                factor_id=factor_id,
                status=status,
                ic_value=metrics["ic_value"],
                rank_ic=metrics["rank_ic"],
                metadata=metadata,
            )
            saved += 1

        # Persist the compact public result first. Generated code and the runner
        # process no longer need to retain database access for post-processing.
        summary = {
            key: value
            for key, value in result.items()
            if key not in {"factors", "data_path"}
        }
        summary.update(
            {
                "persisted_factors": saved,
                "persistence_pending": False,
            }
        )
        import tempfile

        fd, temp_name = tempfile.mkstemp(prefix=".result-", suffix=".tmp", dir=log_dir)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False)
                )
            os.replace(temp_name, result_file)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

        args = SimpleNamespace(
            task_id=task.task_id,
            market=task.market,
            universe=task.universe,
        )
        data_path = ""
        candidate_h5 = log_dir / "git_ignore_folder" / "factor_implementation_source_data" / "daily_pv.h5"
        if candidate_h5.is_file() and not candidate_h5.is_symlink():
            data_path = str(candidate_h5)
        _maybe_spawn_materialize(args, str(log_dir), data_path, saved)
        _maybe_spawn_validate(args, str(log_dir), data_path, saved)
        return summary

    def _collect_results(self, task: EvolutionTask, log_dir: Path) -> dict[str, Any]:
        """Collect results from result.json or log dir."""
        result_file = log_dir / "result.json"
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(result_file, flags)
        except FileNotFoundError:
            fd = None
        except OSError as exc:
            raise RuntimeError(f"Cannot safely open runner result: {exc}") from exc
        if fd is not None:
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
                    raise RuntimeError("Runner result is not a regular file or exceeds 32 MiB")
                with os.fdopen(fd, "r", encoding="utf-8") as handle:
                    fd = -1
                    result = json.load(handle)
                if not isinstance(result, dict):
                    raise RuntimeError("Runner result must be a JSON object")
                return result
            except Exception as exc:
                raise RuntimeError(f"Invalid runner result: {exc}") from exc
            finally:
                if fd not in (None, -1):
                    os.close(fd)
        if task.algorithm == "rdagent":
            raise RuntimeError("RD-Agent runner exited without writing result.json")
        return {
            "total_factors": 0,
            "log_dir": str(log_dir),
            "task_id": task.task_id,
            "market": task.market,
            "message": "Runner did not produce result.json",
        }

    @staticmethod
    def _tail_error_log(log_dir: Path, max_chars: int = 2000) -> str:
        try:
            logs = sorted(
                log_dir.rglob("common_logs.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if not logs:
                stdout = log_dir / "subprocess_stdout.log"
                if not stdout.is_file():
                    return ""
                logs = [stdout]
            with open(logs[0], "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - max_chars * 4))
                return redact_text(f.read().decode("utf-8", errors="replace")[-max_chars:])
        except Exception:
            return ""


# Singleton
_launcher: AlphaAgentLauncher | None = None


def get_launcher() -> AlphaAgentLauncher:
    global _launcher
    if _launcher is None:
        _launcher = AlphaAgentLauncher()
    return _launcher
