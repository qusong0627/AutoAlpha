# RSS 情绪识别（FinBERT）依赖安装说明

## 背景

QuantMind 对 RSS 新闻进行情绪识别（看涨/看跌/中性）时，优先使用 **FinBERT 中文金融情感模型**（比内置词典法更准）。FinBERT 的推断依赖 **PyTorch**（内部调用 `transformers` pipeline 做推理）。

**如果运行环境缺少 PyTorch，FinBERT 会自动降级为词典法**，情绪识别仍然可用，但精度偏低。

> 判断 FinBERT 是否真的生效：在数据库 `news_article_enrichment` 表中，`model_version` 字段带 `+finbert` 后缀即为生效。

***

## 在线部署（默认已带 torch）

代码已在 `docker-compose.yml` 中将默认构建参数改为：

```yaml
args:
  TORCH_DEVICE: ${TORCH_DEVICE:-cpu}          # 默认 cpu，构建时安装 PyTorch CPU 版
  TORCH_CPU_INDEX_URL: ${TORCH_CPU_INDEX_URL:-https://download.pytorch.org/whl/cpu}
```

因此**在线一键部署默认就会安装 PyTorch（CPU 版）**，无需额外操作。

### 若默认构建报错（找不到 torch）

个别网络环境下，阿里云镜像源缺少 `torch==2.9.1+cpu`，请改用官方源重建：

```bash
cd /opt/quantmind
sudo TORCH_DEVICE=cpu \
  TORCH_CPU_INDEX_URL="https://download.pytorch.org/whl/cpu" \
  docker compose build --pull=false quantmind
docker compose up -d
```

### 备选：使用腾讯 PyPI 源装完整版 torch

国内服务器访问官方源较慢时，可改用腾讯镜像源安装**完整版** PyTorch：

```bash
cd /opt/quantmind
sudo TORCH_DEVICE=gpu \
  PIP_INDEX_URL="https://mirrors.cloud.tencent.com/pypi/simple/" \
  PIP_TRUSTED_HOST="mirrors.cloud.tencent.com" \
  docker compose build --pull=false \
    --build-arg PIP_INDEX_URL="https://mirrors.cloud.tencent.com/pypi/simple/" \
    --build-arg PIP_TRUSTED_HOST="mirrors.cloud.tencent.com" \
    quantmind
docker compose up -d
```

> 注意：`TORCH_DEVICE=gpu` 会安装完整版 torch（含 CUDA 组件，镜像体积明显增大，约 24GB）。若机器无 GPU 且追求镜像体积小，优先用 `TORCH_DEVICE=cpu` + 官方 CPU 源。

***

## 离线部署

离线包部署时，`quantmind-oss` 镜像可能来自包内成品镜像，或需要在本机构建。

### 方式一：包内镜像已带 torch（推荐）

确认包内镜像是否含 PyTorch：

```bash
sudo docker run --rm quantmind-oss:latest python3 -c "import torch; print(torch.__version__)"
```

能输出版本号即已带 torch，FinBERT 可直接生效。

### 方式二：包内镜像无 torch，需手工补齐

如果上面命令报 `ModuleNotFoundError: No module named 'torch'`，说明镜像未带 PyTorch。此时有两种补法：

**A. 在线重建带 torch 的镜像（需外网）**

```bash
cd /opt/quantmind
# 确认代码最新（构建参数默认已含 TORCH_DEVICE=cpu）
sudo git pull origin master
sudo TORCH_DEVICE=cpu docker compose build --pull=false quantmind
# 重启服务用新镜像
sudo docker compose up -d
```

**B. 直接向容器内 pip 安装 torch（临时方案，重启后失效）**

> ⚠️ 容器重建后此方式会失效，仅用于临时验证。

```bash
sudo docker exec quantmind pip install "torch==2.9.1+cpu" \
  --index-url https://download.pytorch.org/whl/cpu
```

安装后重启相应服务（或等待 FinBERT 重试冷却期后自动加载）。

***

## 验证 FinBERT 是否生效

### 方法一：查数据库 model\_version

```bash
sudo docker exec quantmind-db psql -U quantmind -d quantmind -c \
  "SELECT model_version, COUNT(*) FROM news_article_enrichment GROUP BY model_version;"
```

若 `model_version` 含 `+finbert` 后缀 → FinBERT 生效。

### 方法二：容器内实际推理

```bash
sudo docker exec quantmind python3 -c "
import torch
from transformers import pipeline
p = pipeline('sentiment-analysis', model='/app/models/finbert-zh-base',
             tokenizer='/app/models/finbert-zh-base', device=-1,
             truncation=True, max_length=256)
print(p('公司发布重大利好公告，净利润大幅增长，看好未来')[0])
"
```

能输出 `{'label': 'LABEL_n', 'score': ...}` 说明 FinBERT 可用；若报 `No module named 'torch'` 则需先按上文补 PyTorch。

***

## 常见问题

| 现象                                                    | 原因                        | 处理                                                                |
| ----------------------------------------------------- | ------------------------- | ----------------------------------------------------------------- |
| `ModuleNotFoundError: No module named 'torch'`        | 镜像未带 PyTorch              | 见上文"离线部署-方式二"，在线重建或容器内 pip 补装                                     |
| `No matching distribution found for torch==2.9.1+cpu` | 当前 pip 源没有该 CPU 版本（如阿里云源） | 改用 `TORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu` 官方源 |
| 情绪全是 neutral / 明显不准                                   | FinBERT 未生效，走了词典法         | 按"验证"章节确认 model\_version 是否带 `+finbert`，缺则补 torch                 |
| 错装成完整版导致镜像很大                                          | 使用了 `TORCH_DEVICE=gpu`    | 无 GPU 时改用 `cpu` + 官方 CPU 源                                        |

***

## 关联文件

* `docker/Dockerfile.oss` — 镜像构建，`TORCH_DEVICE` 决定是否/如何安装 PyTorch

* `docker-compose.yml` — 默认构建参数（`TORCH_DEVICE`、`TORCH_CPU_INDEX_URL`）

* `backend/services/api/news/sentiment.py` — FinBERT 加载与推理（懒加载、失败自动降级词典法）

* `backend/services/api/news/enricher.py` — 情绪融合逻辑与 `model_version` 生成

