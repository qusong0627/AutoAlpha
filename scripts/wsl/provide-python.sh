#!/bin/bash
# 提供 RD-Agent LocalEnv 所需的无前缀 `python`（宿主只有 python3）。
#
# 背景：RD-Agent 的 LocalEnv 以
#   /bin/sh -c 'timeout --kill-after=10 600 python <factor_file>; ...'
# 执行因子代码，PATH = ":/bin/:/usr/bin/:/home/administrator/.local/bin/:<inherited>"。
# Ubuntu 只有 /usr/bin/python3（无 `python`），于是每个候选因子的执行都以
#   /bin/sh: 1: python: not found
# 失败 → CoSTEER 永远收敛不了，coder 阶段无限重试（且持续烧 LLM token）。
#
# 因子代码需要 pandas/numpy/pyarrow（quantdb_data 取数 + to_parquet），因此
# `python` 必须指向 3.12 venv（系统 3.14 没这些包）。用 wrapper 而非裸 symlink：
# 以 venv 真实路径 exec，保证 venv 的 site-packages 生效。
#
# 需以 root 运行（写 /usr/bin）。
set -euo pipefail

VENV_PY=${VENV_PY:-/home/qusong/autoalpha-venv312/bin/python}

if [ ! -x "$VENV_PY" ]; then
  echo "venv python 不可执行：$VENV_PY"
  exit 1
fi

cat > /usr/bin/python <<EOF
#!/bin/sh
exec $VENV_PY "\$@"
EOF
chmod 755 /usr/bin/python

echo "== 结果 =="
ls -la /usr/bin/python
echo
echo "== 以 LocalEnv 的 sanitized PATH 自检 =="
cd /tmp
env -i PATH=":/bin/:/usr/bin/:/home/administrator/.local/bin/" PYTHONPATH=./ \
  /bin/sh -c 'python -c "import sys, pandas, numpy, pyarrow; print(\"exe:\", sys.executable); print(\"prefix:\", sys.prefix); print(\"pandas\", pandas.__version__, \"numpy\", numpy.__version__, \"pyarrow\", pyarrow.__version__)"'
