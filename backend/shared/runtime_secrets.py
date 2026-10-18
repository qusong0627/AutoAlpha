"""从环境变量或私有 ``runtime.env`` 文件读取运行时密钥。

优先级：非空进程环境变量 > 私有 runtime.env > 默认值。环境文件仅是服务端环境的
受保护来源，不是用户 Profile 或浏览器可写的密钥存储。当前因子挖掘入口的配置 API
只读；不要将 ``set_secret`` / ``delete_secret`` 接入免登录 HTTP 路由。

POSIX 上 runtime.env 必须由服务用户拥有、文件权限为 0600 或更严格，且父目录不可被
组/其他用户写入；符号链接和非普通文件会被拒绝。请把密钥文件放在服务用户的私有目录。
"""

from __future__ import annotations

import logging
import os
import re
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


def runtime_env_path() -> Path:
    override = os.getenv("QM_RUNTIME_ENV_FILE", "").strip()
    if override:
        return Path(override)
    return _PROJECT_ROOT / "config" / "runtime.env"


def _read_secure_text(path: Path) -> str | None:
    try:
        if path.is_symlink() or path.parent.is_symlink():
            raise PermissionError(f"runtime env 文件及其目录不能是符号链接: {path}")
        if os.name == "posix":
            parent = path.parent.stat()
            if parent.st_uid != os.getuid() or parent.st_mode & 0o022:
                raise PermissionError(
                    f"runtime env 目录必须由当前用户拥有且不可被组/其他用户写入: {path.parent}"
                )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        # In particular, O_NOFOLLOW rejects a symlink on POSIX. Never follow a
        # runtime-secret path to a target that can be swapped independently.
        raise PermissionError(f"拒绝打开不安全的 runtime env 文件: {path}") from exc

    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError(f"runtime env 必须是普通文件: {path}")
        if os.name == "posix":
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise PermissionError(
                    f"runtime env 必须由当前用户拥有且权限为 0600 或更严格: {path}"
                )
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            content = stream.read()
    finally:
        if fd >= 0:
            os.close(fd)
    return content


def _write_secure_text(path: Path, content: str) -> None:
    """原子写入 0600 文件，避免跟随符号链接或短暂暴露宽权限文件。"""
    temp_fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="\n") as stream:
            temp_fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
        if os.name == "posix":
            os.chmod(path, 0o600)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _parse(path: Path) -> dict[str, str]:
    content = _read_secure_text(path)
    if content is None:
        return {}

    out: dict[str, str] = {}
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def load_runtime_env() -> int:
    """将 runtime.env 注入进程环境变量，返回注入条数。"""
    try:
        entries = _parse(runtime_env_path())
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 runtime.env 失败: %s", exc)
        return 0
    loaded = 0
    for key, value in entries.items():
        if not os.environ.get(key, "").strip():
            os.environ[key] = value
            _injected_keys.add(key)
            loaded += 1
    return loaded


# 本进程启动时由 runtime.env 注入的键。这类键的权威来源是 runtime.env
# （管理台随时可改），get_secret 对它们实时重读，避免启动期快照过期。
_injected_keys: set[str] = set()


def get_secret(key: str, default: str = "") -> str:
    """读取密钥的当前值：管理台改 key 后无需重启进程即生效。

    优先级与模块文档一致（真实环境变量 > runtime.env > default），
    唯一例外：键是本进程启动时由 runtime.env 注入的（真实环境变量为空），
    则每次调用实时重读 runtime.env——启动注入的快照不构成优先级。
    典型场景：Celery worker 常驻运行，用户在管理台换 QuantDB API Key，
    下一次定时同步任务即用新 key。
    """
    try:
        if key in _injected_keys:
            fresh = _parse(runtime_env_path()).get(key, "").strip()
            if fresh:
                return fresh
        env_val = os.environ.get(key, "").strip()
        if env_val:
            return env_val
        return _parse(runtime_env_path()).get(key, "").strip() or default
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_secret(%s) 读取失败，回退进程环境变量: %s", key, exc)
        return os.environ.get(key, "").strip() or default


def set_secret(key: str, value: str) -> Path:
    """写入/更新一条密钥，同时立即生效于当前进程。"""
    if not _KEY_PATTERN.match(key):
        raise ValueError(f"非法的配置键名: {key}")
    if "\n" in value or "\r" in value:
        raise ValueError("配置值不能包含换行")

    path = runtime_env_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.parent.is_symlink():
        raise PermissionError(f"runtime env 目录不能是符号链接: {path.parent}")
    if os.name == "posix":
        parent_stat = path.parent.stat()
        if parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o022:
            raise PermissionError(
                f"runtime env 目录必须由当前用户拥有且不可被组/其他用户写入: {path.parent}"
            )

    existing = _read_secure_text(path)
    lines = existing.splitlines() if existing is not None else []
    new_line = f"{key}={value}"
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)

    _write_secure_text(path, "\n".join(lines) + "\n")
    os.environ[key] = value
    return path


def read_runtime_env() -> dict[str, str]:
    """runtime.env 的当前内容（**不含**进程环境变量的覆盖）。"""
    return _parse(runtime_env_path())


def delete_secret(key: str) -> bool:
    """删除一条密钥：从 runtime.env 抹掉，并立即从本进程环境中移除。

    返回「本进程现在还看不看得见它」。**真实环境变量不删**——那是运维在
    docker-compose / 启动脚本里声明的，进程内删掉只会制造「这个进程看不见、别的进程
    还看得见」的假象（而调用方会以为删干净了）；这种情况打警告并返回 ``False``。

    与 ``set_secret`` 对称：空值在本模块的语义里等于「未配置」（见模块 docstring），
    所以删掉后重启不会把它再注入回来。
    """
    if not _KEY_PATTERN.match(key):
        raise ValueError(f"非法的配置键名: {key}")

    path = runtime_env_path()
    removed_from_file = False
    existing = _read_secure_text(path)
    if existing is not None:
        lines = existing.splitlines()
        kept = [line for line in lines if not line.strip().startswith(f"{key}=")]
        if len(kept) != len(lines):
            _write_secure_text(path, "\n".join(kept) + ("\n" if kept else ""))
            removed_from_file = True

    was_injected = key in _injected_keys
    _injected_keys.discard(key)
    if removed_from_file or was_injected:
        os.environ.pop(key, None)
        return True
    if os.environ.get(key, "").strip():
        logger.warning(
            "delete_secret(%s)：runtime.env 里没有它，但本进程环境里有（真实环境变量，"
            "由编排/启动脚本注入）。未做任何删除——要真正下线请改那里。",
            key,
        )
        return False
    os.environ.pop(key, None)  # 空串按「未配置」处理
    return True


def mask_secret(value: str | None) -> str:
    """脱敏展示，永不回传明文。"""
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}{'*' * 8}{value[-4:]}"
