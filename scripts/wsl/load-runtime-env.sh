#!/bin/bash
# Source this file from WSL entrypoints. Existing non-empty process variables win;
# otherwise values from a private runtime.env are exported into the caller.

QM_ENV_SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
QM_ENV_REPO_ROOT=$(cd -- "$QM_ENV_SCRIPT_DIR/../.." && pwd)
QM_ENV_DEFAULT_FILE="$QM_ENV_REPO_ROOT/config/runtime.env"
if [ -f "$HOME/.config/autoalpha/runtime.env" ]; then
  QM_ENV_DEFAULT_FILE="$HOME/.config/autoalpha/runtime.env"
fi
QM_ENV_FILE=${ENV_FILE:-${QM_RUNTIME_ENV_FILE:-$QM_ENV_DEFAULT_FILE}}
export QM_RUNTIME_ENV_FILE="$QM_ENV_FILE"

if [ -f "$QM_ENV_FILE" ]; then
  if [ -L "$QM_ENV_FILE" ]; then
    echo "FATAL: env 文件不能是符号链接：$QM_ENV_FILE" >&2
    return 1
  fi
  if LC_ALL=C grep -q $'\r' "$QM_ENV_FILE"; then
    echo "FATAL: env 文件含 CR 字符；请使用 LF 行尾保存后重试：$QM_ENV_FILE" >&2
    return 1
  fi
  if ! bash -n "$QM_ENV_FILE"; then
    echo "FATAL: env 文件语法错误：$QM_ENV_FILE" >&2
    return 1
  fi
  QM_ENV_MODE=$(stat -c '%a' "$QM_ENV_FILE" 2>/dev/null || true)
  QM_ENV_OWNER=$(stat -c '%u' "$QM_ENV_FILE" 2>/dev/null || true)
  QM_ENV_PARENT=$(dirname "$QM_ENV_FILE")
  QM_ENV_PARENT_MODE=$(stat -c '%a' "$QM_ENV_PARENT" 2>/dev/null || true)
  QM_ENV_PARENT_OWNER=$(stat -c '%u' "$QM_ENV_PARENT" 2>/dev/null || true)
  QM_ENV_UID=$(id -u)
  if [[ ! "$QM_ENV_MODE" =~ ^[0-7]{3,4}$ ]] || (( (8#$QM_ENV_MODE & 077) != 0 )) || [ "$QM_ENV_OWNER" != "$QM_ENV_UID" ]; then
    echo "FATAL: env 文件必须由当前用户拥有且权限不宽于 0600：$QM_ENV_FILE" >&2
    return 1
  fi
  if [ -L "$QM_ENV_PARENT" ] || [[ ! "$QM_ENV_PARENT_MODE" =~ ^[0-7]{3,4}$ ]] || (( (8#$QM_ENV_PARENT_MODE & 022) != 0 )) || [ "$QM_ENV_PARENT_OWNER" != "$QM_ENV_UID" ]; then
    echo "FATAL: env 文件目录必须由当前用户拥有且不可被组/其他用户写入：$QM_ENV_PARENT" >&2
    return 1
  fi

  while IFS= read -r -d '' QM_ENV_ENTRY; do
    QM_ENV_KEY=${QM_ENV_ENTRY%%=*}
    QM_ENV_VALUE=${QM_ENV_ENTRY#*=}
    QM_ENV_CURRENT=$(printenv -- "$QM_ENV_KEY" 2>/dev/null || true)
    if [[ "$QM_ENV_KEY" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && [ -z "$QM_ENV_CURRENT" ] && [ -n "$QM_ENV_VALUE" ]; then
      export "$QM_ENV_KEY=$QM_ENV_VALUE"
    fi
  done < <(set -a; . "$QM_ENV_FILE"; env -0)
fi

# Allow a full connection URL as the source of truth while keeping psql-based
# diagnostics on the same credential; never print the decoded password.
if [ -z "${DB_PASSWORD:-}" ] && [ -n "${DATABASE_URL:-}" ]; then
  DB_PASSWORD=$(python3 -c 'import os, urllib.parse; print(urllib.parse.unquote(urllib.parse.urlsplit(os.environ["DATABASE_URL"]).password or ""))')
  export DB_PASSWORD
fi

unset QM_ENV_SCRIPT_DIR QM_ENV_REPO_ROOT QM_ENV_DEFAULT_FILE QM_ENV_MODE QM_ENV_OWNER QM_ENV_CURRENT
unset QM_ENV_PARENT QM_ENV_PARENT_MODE QM_ENV_UID QM_ENV_ENTRY QM_ENV_KEY QM_ENV_VALUE QM_ENV_FILE
