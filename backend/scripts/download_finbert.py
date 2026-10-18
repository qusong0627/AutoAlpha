"""下载 FinBERT-zh 中文情感模型的离线权重到 models/finbert-zh-base。

用途：新装/升级环境补齐新闻情感增强模型（backend/services/api/news/sentiment.py 使用）。
缺失时系统静默降级为纯词典法情感，不报错但情感分维度打折，因此安装链路必须显式执行本脚本。

用法:
  python3 backend/scripts/download_finbert.py            # 默认写入 <repo>/models/finbert-zh-base
  FINBERT_ZH_MODEL=/app/models/finbert-zh-base python3 backend/scripts/download_finbert.py
      # 容器内路径也可，按该环境变量定位目标目录

幂等：已存在且大小一致的文件自动跳过；断点文件 *.part 会覆盖重下。
源站顺序：魔搭社区 ModelScope（国内首选）→ hf-mirror.com → huggingface.co（兜底）。
可通过环境变量 MODELSCOPE_ENDPOINT 覆盖魔搭社区基址（默认 https://www.modelscope.cn）。
"""
from __future__ import annotations

import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ID = "bardsai/finance-sentiment-zh-base"
# 权重只需 safetensors 一种；bin/h5/onnx 均不需要
FILES = [
    "config.json",
    "model.safetensors",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
]
# 魔搭社区（ModelScope）URL 结构为 /models/{repo}/resolve/master/{file}，
# 与 HuggingFace /resolve/main/ 不同，因此单独维护；若该模型在魔搭上不存在会
# 返回 404 并自动跳过，不影响后续源。
MODELSCOPE_ENDPOINT = (
    os.getenv("MODELSCOPE_ENDPOINT", "https://www.modelscope.cn").rstrip("/")
)
ENDPOINTS = [
    os.getenv("HF_ENDPOINT", "").rstrip("/") or None,
    "https://hf-mirror.com",
    "https://huggingface.co",
]


def _default_dest() -> Path:
    env = os.getenv("FINBERT_ZH_MODEL", "")
    if env:
        return Path(env)
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "models" / "finbert-zh-base"


def _download_one(url: str, dest_file: Path) -> bool:
    tmp = dest_file.with_suffix(dest_file.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as out:
            expected = r.headers.get("Content-Length")
            shutil.copyfileobj(r, out)
        if expected and str(tmp.stat().st_size) != expected:
            print(f"  ⚠️ {dest_file.name} 大小不符 ({tmp.stat().st_size}/{expected})，重试")
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(dest_file)
        print(f"  ✅ {dest_file.name} {dest_file.stat().st_size / 1024 / 1024:.1f} MB")
        return True
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        print(f"  ⚠️ {dest_file.name} 失败: {str(e)[:120]}")
        tmp.unlink(missing_ok=True)
        return False


def main() -> int:
    dest = _default_dest()
    dest.mkdir(parents=True, exist_ok=True)

    missing = [f for f in FILES if not (dest / f).exists() or (dest / f).stat().st_size == 0]
    if not missing:
        total = sum((dest / f).stat().st_size for f in FILES)
        print(f"✅ FinBERT 模型已就绪: {dest} ({total / 1024 / 1024:.1f} MB)，跳过下载")
        return 0

    print(f"FinBERT 模型缺失 {len(missing)} 个文件 → 下载到 {dest}")
    fails: dict[str, int] = {}
    for name in missing:
        ok = False

        # 1) 优先尝试魔搭社区 ModelScope（国内网络通常更稳定）
        if MODELSCOPE_ENDPOINT:
            ms_url = (
                f"{MODELSCOPE_ENDPOINT}/models/{REPO_ID}/resolve/master/{name}"
            )
            print(f"· {name} ← ModelScope ({MODELSCOPE_ENDPOINT})")
            for attempt in range(2):
                if _download_one(ms_url, dest / name):
                    ok = True
                    break
                time.sleep(1 * (attempt + 1))

        # 2) 回退到 HuggingFace 系列端点
        if not ok:
            for ep in [e for e in ENDPOINTS if e] or ["https://hf-mirror.com"]:
                url = f"{ep}/{REPO_ID}/resolve/main/{name}"
                print(f"· {name} ← {ep}")
                for attempt in range(2):
                    if _download_one(url, dest / name):
                        ok = True
                        break
                    time.sleep(2 * (attempt + 1))
                if ok:
                    break

        if not ok:
            fails[name] = fails.get(name, 0) + 1

    if fails:
        print(f"\n❌ 以下文件仍未获取: {sorted(fails)}")
        print("   新闻情感将降级为纯词典法；可稍后重跑本脚本。")
        return 1
    total = sum((dest / f).stat().st_size for f in FILES)
    print(f"\n🎉 FinBERT 就绪: {dest} ({total / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
