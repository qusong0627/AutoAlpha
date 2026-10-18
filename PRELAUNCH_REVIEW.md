# AutoAlpha 因子挖掘模块 — 上线前全面审查报告

- **审查对象**：`D:\git\autoalpha`（`quantmindoss@next` 因子挖掘切片的独立部署镜像）
- **审查时间**：2026-10-18 19:56 – 20:15（Asia/Shanghai）
- **审查方式**：静态代码审查 + 运行时实证（启动 PG/后端/前端、发真实 HTTP 请求、受控 PoC）
- **结论**：**当前状态不可对外上线。** 存在 4 项 P0 阻塞问题，其中 2 项已取得可复现的实证（未认证 RCE、未认证全站 DoS），另有 1 项已泄漏的真实密钥必须先吊销。
  若严格限定为"仅本机开发"，也**必须先轮换已泄漏的密钥**（P0-1/P0-2），否则任何能读到本仓库的人都持有可用凭据。

## 修复推进记录（2026-10-18）

本报告下方的发现是审查时的基线，不应再被当作当前代码状态。后续已在镜像内落地部分修复；目前只完成静态语法/配置检查，没有重跑后端、前端或端到端验证。

- 已修：表达式严格白名单及 GET 提取路径去除 `exec`；promote 重计算移出事件循环并加超时/并发上限；内部密钥默认值 fail-closed，Vite 秘密兜底移除且仅监听回环。
- 已修：任务日志私有权限、子进程环境裁剪、任务终结写库重试、回测请求上限、因子子进程 bubblewrap 隔离、遗留任务进程组身份记录。
- 已修：Python 3.12 启动检查、bcrypt 兼容版本、Python 哈希锁、前端 npm 锁安装与 registry、关键运维脚本 fail-fast/破坏性操作保护。
- 本轮补充：免登录模式校验 loopback `Host`，并拒绝写请求的非本机/opaque `Origin` 与标记为 cross-site 的无 Origin 请求，降低浏览器 DNS rebinding/跨站请求风险；`ENVIRONMENT=production|staging` 时拒绝以 `AUTH_DISABLED=1` 启动。这些措施不认证本机进程，也不替代网络边界。
- 本轮补充：挖掘 LLM、Embedding、MinerU 凭证改为只从服务端环境读取；Profile/Redis 中的旧值不再用于挖掘，在线保存接口返回 409，设置页改为只读。旧 Profile/Redis 数据尚未清理；MinerU 有显式 DELETE 路径，Profile 历史数据仍需由其管理方清理。
- 本轮补充：MinerU 旧 Redis 设置存储层不再读取历史密钥，旧设置对象也拒绝转换为运行凭证；仅保留按身份删除旧 Redis 记录的路径。
- 本轮补充：移除未使用的原始 Profile 凭证网关；文档解析不再保留每用户配置缓存/客户端路径。旧任务行若标记为用户密钥来源会明确失败并提示按部署环境重新上传。
- 本轮只读扫描：在 1,321 个仓库文件中未发现匹配 `sk-...` 形式的 API Key 字面量；扫描不覆盖 Git 历史、外部 Redis、WSL 任务日志或供应商侧状态，因此旧 DeepSeek Key 仍须吊销。
- 本轮只读数据盘点：当前 `quantmind` 数据库的 `public` schema 未发现 `user_profiles` 或 API key/token/secret 字段；`rd_agent_docs` 为 0 行。本机 WSL `redis-server` 未运行且没有 `REDIS_*` 环境变量，Docker daemon 也不可用，因此无法核验停止的 Redis 数据目录、Docker/外部 Redis 或历史快照；不能据此宣称历史凭证已清除。
- 本轮序列化工件清理：新增 `scripts/wsl/audit-serialized-secrets.py`，以 GNU grep 对 pickle/joblib 字节做只读扫描，不反序列化且不输出匹配内容。扫描发现 5 个 `LITELLM_SETTINGS` 配置快照命中凭证字段，其中 4 个命中常见密钥格式；这 5 个不被应用读取的历史配置快照已删除。复扫剩余 1,242 个序列化文件，未命中脚本覆盖的凭证字段、赋值模式或常见密钥格式。未知编码/封装方式下的裸密钥仍不能由启发式扫描排除。
- 本轮运行时凭证清理：runner 退出后，launcher 在结果收集前删除本任务内 `LITELLM_SETTINGS` 目录；服务启动恢复时，也会先识别并停止遗留 runner，再清理其设置快照。删除操作仅作用于任务日志根内、经 `lstat` 确认为真实目录的目标，不反序列化。
- 本轮补充：新增/扩展 `scripts/wsl/init-runtime-env.sh`，以隐藏输入方式收集服务端 LLM Key 与 PostgreSQL 密码，生成缺失的随机 `INTERNAL_CALL_SECRET`/`SECRET_KEY`，支持原子创建及 `--complete` 安全补全；补全只静态识别现有赋值行，不执行现有文件内容。本轮只读检查发现现有文件父目录 0700、文件 0600、属主 qusong，但仅包含非空 `INTERNAL_CALL_SECRET` 与 `SECRET_KEY`；没有 LLM Key、`DB_PASSWORD` 或 `DATABASE_URL`，因此启动前仍须由操作者补齐。
- 本轮补充：扩充 `runtime.env.example` 的可选服务凭证目录，覆盖因子命名、QuantDB、数据源、ModelScope、iFinD、腾讯 COS 与 QQ Bot 等现有环境变量入口；模板值保持注释，避免空值覆盖外部环境。
- 本轮凭证输出修复：`GET /llm-config` 的 chat/embedding endpoint 状态仅返回 scheme+host+port，丢弃 userinfo、路径、query、fragment；MinerU cloud/local base URL 均拒绝内嵌凭证/query/fragment，且无效配置日志不再打印原始 URL。
- 本轮文档修正：清理方向拆解、文档整理、因子代码生成和 LLM 客户端中“用户 Profile Key 优先”的过时说明，明确当前挖掘部署仅使用服务端环境变量。重建镜像清单后，共同文件差异更新为 74 个（23 个仅换行、51 个内容差异），镜像独有文件 72 个；上游工作树状态为空。
- 本轮数据库迁移核验：尝试以 `quantmind` 做只读 schema 查询，因本机 PostgreSQL peer 认证拒绝而未能查看列/索引；非交互 `sudo -u postgres` 也因需交互认证失败。未对数据库做任何修改，backend systemd unit 当前 inactive，因此租约迁移与运行态行为仍待有权限的数据库会话验证。
- 本轮环境与版本控制核验：本地 `web/.env.development.local` 只有 `VITE_*` 地址/模式/免登录开关等变量名，没有 API 凭证变量，且已由 `.gitignore` 排除；环境示例仍可跟踪。原本已初始化但 0 文件、无 HEAD 的本地仓库现建立基线提交 `9c0bd3b`（1,316 文件）；没有 Git remote。已对提交内容做常见密钥格式扫描，未命中。PostgreSQL TCP 密码认证也因未配置可用密码而拒绝，迁移未执行。
- 本轮网络运行态复核：WSL Ubuntu 是 WSL2，未配置用户 `.wslconfig` 或 `netsh portproxy`；Windows 侧 8001 仅观测到 `127.0.0.1` 的 `wslrelay` 监听。WSL 内仍有手动启动的 Uvicorn（PID 1557，监听 `0.0.0.0:8001`），systemd unit 显示 inactive；`GET /health` 返回 healthy，`auth_disabled=true`、`startup_ddl=true`、9 组/111 条路由已挂载。Windows 防火墙各 profile 已启用，但入站动作显示 NotConfigured；loopback 监听和无 portproxy 不能证明所有 WSL/容器/网络路径均不可达。未终止这个非本轮启动的后端进程。
- 本轮私有环境补全未完成：已尝试通过 Codex PTY 启动 `init-runtime-env.sh --complete`，但终端面板返回 queued、当前可见终端未切换；脚本停在 LLM 变量名提示时已中止并释放锁，没有输入密钥或写文件。复查 `runtime.env` 仍为 `0600`、属主 `qusong`，只含非空 `INTERNAL_CALL_SECRET` 与 `SECRET_KEY`；无临时文件。需在可见的 WSL 终端完成隐藏输入，再重启手动启动的后端以加载新环境。
- 本轮启动路径修复：`start-dev.sh` 与 `load-runtime-env.sh` 使用了 Bash 不支持的 `${!name:-}` 间接参数默认展开，会在读取变量时触发 `invalid indirect expansion`。已改为按变量名调用 `printenv`；环境加载器与补全脚本还会拒绝 CR 行尾，避免 Windows CRLF 混入凭证。用当前受保护的 runtime.env 实际加载后仅确认内部/JWT 两个变量，不输出值。
- 本轮补充：新增 PostgreSQL custom-format 备份、SHA256 校验、保留期清理、夜间 systemd timer 模板和 scratch DB 恢复演练脚本。只读检查发现 PGDATA 在 `/dev/sdd` ext4；E: 虽为另一挂载且有约 281 GB 可用，但 WSL 挂载类型为 9p/DrvFS，当前模式显示 `0777`，不满足私有备份目录要求。timer 未启用或实跑；QuantDB 副本仍未实现。
- 本轮补充：挖掘任务表增加进程 owner 与租约截止时间；进度只续当前进程持有的活动任务，终态写入受 owner/活动状态约束，启动及每 30 秒回收过期租约。
- 本轮补充：新增服务端密钥值与常见 Bearer/API-key 形式脱敏；ASGI 日志处理器、HTTP 异常、直接错误响应、任务状态/历史行、文档错误字段与 `subprocess_stdout.log` 写入/读取均经过脱敏。RD-Agent 自行创建的 `common_logs.log` 在 runner 退出后也会流式脱敏并原子替换；历史序列化工件已扫描清理，后续 runner 收尾会删除 `LITELLM_SETTINGS` 快照。任务目录仍以 0700 私有权限隔离。
- 本轮运行态修复：将现有 WSL `/data/alpha_agent_logs` 及全部一级任务目录从 0755 改为 0700；实查没有权限不符的一级任务目录。随后扫描删除了 5 个命中凭证标记的旧 `LITELLM_SETTINGS` pickle。
- 本轮补充：新增本机后端 `Restart=always` systemd unit、每分钟 `/health` 探针 timer 和 journal critical 失败记录模板；`/health` 现含限时 PostgreSQL `SELECT 1` 与核心启动 DDL 状态；unit 尚未安装/启用，不代替数据库写入探测或外部通知。
- 本轮补充：独立入口接入任务日志 90 天留存 GC，启动和每日执行一次，只清终态超龄目录；清理前拒绝根/任务目录符号链接并确保目录权限 0700。服务 stderr 由 journald 收集；本轮实测 journal 占用 872.2 MB，系统配置未显式设置 `SystemMaxUse` 或 `MaxRetentionSec`，具体上限仍由 systemd 默认策略决定。
- 保持未完成：DeepSeek 旧 Key 必须在服务商控制台吊销；备份目标配置、timer 启用与恢复演练、守护/告警、生产反代、上游差异重建和运行态验收仍需处理。租约代码尚未对本机数据库运行迁移/运行验证。
- 当前认证决策：仅本机可信开发环境保持 `AUTH_DISABLED=1`；Vite 默认只监听 `127.0.0.1`。该设置不构成生产免认证方案。

## 当前部署决策补充（2026-10-18）

用户确认：API 凭证由服务端环境变量提供；当前阶段不启用用户登录认证。挖掘 LLM、Embedding、MinerU 的运行时解析现只读服务端环境；Profile/Redis 历史凭证不再参与挖掘，在线写入接口已禁用。`config/runtime.env.example` 是模板，实际密钥应放在 WSL ext4 的私有目录 `~/.config/autoalpha/runtime.env`（权限 `0600`），不要放在 D: 仓库目录、前端 `VITE_*` 变量或提交记录中。脚本已接入该文件并在缺少必填凭证时拒绝启动。

这项决策把部署范围限定为**单用户、本机开发**。`AUTH_DISABLED=1` 下所有 API 调用都拥有开发管理员身份，因此环境变量只能保护凭证的来源，不能隔离同机调用者或不同操作者；CORS 也不是认证或 CSRF 防护。按此范围，建议优先完成：

1. **吊销已泄漏的旧 DeepSeek Key 并换新**，随后确认新 Key 只存在于受保护的运行时环境；这是当前唯一不能靠代码改动替代的 P0。
2. **确认网络边界**：Vite 只监听回环；后端为 Windows→WSL localhost 转发需要绑定 WSL 接口，需检查 Windows 端口转发/防火墙，且不要把无认证实例接入局域网、隧道或公网。后端现已在免登录模式校验 loopback `Host`，并拒绝写请求的非本机/opaque `Origin` 与标记为 cross-site 的无 Origin 请求。这降低浏览器 DNS rebinding/跨站请求风险，但不认证本机进程，也不阻止能直接连接 WSL 端口并伪造 Host 的非浏览器客户端。
3. **完成凭证生命周期**：实际生成并配置随机 `INTERNAL_CALL_SECRET`（它在认证关闭时仍是服务间凭证，开启认证时又可铸造管理员身份）；限制运行时文件权限；继续检查日志、异常和诊断输出是否会泄漏 Key。ASGI 日志、子进程 stdout、任务结束后的 RD-Agent `common_logs.log` 和 API 错误返回已加脱敏；序列化工件已按已知凭证模式扫描并删除 5 个旧设置快照，未知编码形式仍无法由字节特征扫描排除。
4. **补齐数据与进程恢复能力**：Postgres 定期备份并做恢复演练、QuantDB 副本。PGDATA 位于 WSL ext4；E: 是独立 9p/DrvFS 挂载（约 281 GB 可用），但当前显示 `0777`，不能作为脚本要求的私有 POSIX 备份目录。需要配置受限权限的独立 POSIX 盘或异机目标。挖掘任务现有进程 owner、180 秒租约与 30 秒周期清扫；服务启动会在租约过期后原子接管本机仍有状态文件的 queued 任务并排水，但尚未完成数据库迁移/运行态验证。
5. **完成本机运维可观测性**：应用守护/重启策略、`/health` 告警、日志轮转和磁盘空间告警。部署前至少做一次后端启动、前端代理、数据库读写和关键挖掘路径的运行态验收；本次修复后尚未重跑这些检查。

由于当前明确不提供用户认证，登录路由、面向外部访问的生产反代与生产前端部署先作为**开放服务前置条件**，不应被误记为当前本机开发的已完成能力。若之后要多人使用或跨机器访问，必须先启用可信认证并按角色保护管理路由，同时补齐 CSRF/Origin 策略、审计和限流，再开放网络入口。

---

## 一、审查期间的环境与操作记录

| 项目 | 审查前状态 | 审查后状态 |
|---|---|---|
| PostgreSQL 18（WSL） | 运行中（16 表 / 13 挖掘任务） | 未改动，仍运行 |
| 后端 8001 | **未运行** | 我启动过用于验证，**现已停止**（端口已释放） |
| 前端 3000 | 有一个 vite 在跑，但来自 **`D:\git\quantmindoss`（上游仓库）** | 未改动，仍在跑 |
| `web/dist` | 已存在 | 被子审查进程执行 `npm run build` **重建**（18 文件 / 1873 KB） |

**我对系统做过的写操作**：启动/重启后端（现已全部 `kill -9` 清理，无残留进程）、执行一次 `npm run build` 重建 `web/dist`、在 `/tmp` 与 Windows `%TEMP%` 下创建验证脚本（`/tmp` 下的标记文件已清理）。**未修改任何仓库文件**。

**关于 3000 端口**：当前占用 3000 的 vite 进程命令行指向 `D:\git\quantmindoss\node_modules\...\vite.js`，即**上游仓库**，不是本镜像。因此按文档执行 `scripts\dev-web.cmd` 会因端口占用直接失败（`Error: Port 3000 is already in use`）。而 `scripts/verify-all.ps1` 实测该前端的**所有 `/api` 请求返回 500 / 0 字节**——也就是说，现在浏览器上看到的"应用"既不是本镜像，API 也是坏的。

**⚠ 需另行关注的异常：上游仓库在我审查期间被第三方改动。**
`D:\git\quantmindoss` 的 `git status --porcelain` 在审查开始（19:57）时是**干净的**，审查结束时变为 **4 个脏文件**，修改时间全部落在我的审查窗口内：

| 文件 | 修改时间 |
|---|---|
| `electron/package.json` | 20:09:29 |
| `electron/src/pages/trading/tabs/ManualTaskPage.tsx` | 20:07:51 |
| `electron/src/styles/global.css` | 20:07:47 |
| `package-lock.json` | 20:10:09 |

**这不是我造成的**：我对 `quantmindoss` 只执行过只读 git 命令（`status` / `branch` / `log` / `rev-parse` / `archive`），`archive` 的产物写在我的临时目录。但两点值得警惕：① 该仓库正被**并发写入**（很可能是另一个会话/编辑器在改交易页并跑了 `npm install`）；② 它既不符合 AGENTS.md 所述的"保持 5 个既有脏文件"，也不符合我审查开始时的"干净"，说明上游工作区状态正在**独立漂移**。若有人正基于它重新 archive 本镜像，provenance 会再次改变。
（说明：本报告的镜像完整性比对是拿 `git archive next` 的**提交对象树**做的，不受工作区脏文件影响。）

---

## 二、P0 — 阻塞上线

### P0-1 未认证远程代码执行（已实证）

**位置**：`backend/services/api/routers/admin/alpha_factor_pipeline.py:161`
**下沉点**：`qlib/data/data.py:397` → `expression = eval(parse_field(field))`（无 `__builtins__` 限制）

```python
# alpha_factor_pipeline.py:155-165
def compute_factor_via_qlib(expression: str, feature_name: str) -> pd.DataFrame:
    _ensure_qlib_init()
    from qlib.data import D
    instruments = D.instruments(market="csi300")
    df = D.features(instruments, [expression], freq="day")   # ← 请求体里的表达式直接进 eval
```

**门禁形同虚设**（`:113-121`）——只要表达式含 `(` 就放行：

```python
qlib_indicators = ["$", "Ref(", "Mean(", ..., "(", ")", "?", ":", "&&", "||"]
return any(ind in expr for ind in qlib_indicators)
```

**两条可达路径**：

| 路径 | 表达式来源 | 位置 |
|---|---|---|
| `POST /api/v1/admin/alpha-factors/promote-by-expression` | 请求体 `factors[].expression` | `:436` → `:453` → `:459` |
| `POST /api/v1/admin/alpha-factors/promote`（读 DB） | `rd_agent_factors.metadata.formulation` / `factor_code` | `:332` → `:345` |

**实证结果**（在真实函数 `compute_factor_via_qlib` 中执行，非模拟）：

```
EXPR: Operators.__init__.__globals__['__builtins__']['__import__']('os').__dict__['system']('id>/tmp/e2e_id_proof.txt')
EXC: AttributeError 'int' object has no attribute 'get_extended_window_size'
EXISTS: True   SIZE: 179
CONTENT: uid=1000(qusong) gid=1000(qusong) groups=1000(qusong),...,27(sudo),...,1001(docker)
```

`qusong` 同时属于 **`sudo`** 与 **`docker`** 组 → 该 RCE 等价于 WSL 宿主 root。

**载荷构造要点**（务必照此修复，否则会误判为"打不通"）：

1. `parse_field` 会把 `名字(` 改写为 `Operators.名字(`，所以 `__import__('os')`、`os.system(...)` 这类**函数调用写法会被破坏**；改用下标形式 `['__import__'](...)` / `['system'](...)` 即可绕过（`(` 前是 `]`，正则不匹配）。
2. qlib 在 eval 前会剥掉**所有空白字符**（`qlib/data/cache.py:502,539` 的 `remove_fields_space`）。所以含空格的命令会变成 `cp/etc/hostname/tmp/x`。这是**唯一的偶然阻力**，用无空格载荷（如 `id>文件`）即可。审查中那个 0 字节文件正是"重定向先建文件、命令因空格被吞而不存在"造成的假象。
3. eval 命名空间里就有 `Operators` 与 `__builtins__`（实测），无需任何前置条件。

**修复**：不要在主进程对请求表达式调用 `D.features`。仓库里已有隔离子进程实现 `_run_expression_factor_subprocess`（`backend/services/engine/routers/alpha_agent.py:5064`，带 `timeout=600`），或改用白名单 DSL 解析（`factorminer.dsl`）彻底去掉 eval。同时把 `_is_qlib_expression` 从"含 `(` 即通过"改为真正的语法白名单。

---

### P0-2 仓库内公开的 `INTERNAL_CALL_SECRET` 是一枚可用的管理员凭据（已实证）

**问题**：`autoalpha-dev-internal-secret-9f3a` 硬编码在 **7 个文件**里，且**不在** `backend/shared/auth.py:25-27` 的 `_PUBLIC_INTERNAL_DEFAULTS`（该集合只有 3 个值）——于是它被当成"已配置的真实密钥"**接受**。

出现位置：`scripts/wsl/start-dev.sh:16`、`scripts/dev-web.cmd:16`、`web/vite.config.ts:43`、`scripts/wsl/task-probe.sh:6`、`scripts/wsl/smoke.sh:18`、`scripts/wsl/quantdb-check.sh:6`、`scripts/wsl/db-smoke.sh:6`。

`scripts/wsl/start-dev.sh:14-15` 的注释把逻辑写反了：它说"必须避开 `_PUBLIC_INTERNAL_DEFAULTS`"——恰恰相反，**不在**该集合里才是它可用的原因。

**放大器**：`web/vite.config.ts:41-47` 会给**每一个** `/api` 请求自动注入身份头，且 `server.host: '0.0.0.0'`：

```ts
proxyReq.setHeader('X-Internal-Call', process.env.INTERNAL_CALL_SECRET || 'autoalpha-dev-internal-secret-9f3a');
proxyReq.setHeader('X-User-Id', process.env.DEV_USER_ID || '10000001');
```

**实证**（后端以 `AUTH_DISABLED=0` 启动，即文档所说的"生产形态"）：

| # | 请求 | 结果 |
|---|---|---|
| a | 无任何凭据 | **401** ✅ 认证本身有效 |
| b | 仅加上述两个头 | **200 + 25605 字节管理员数据**（含真实挖掘任务历史、`user_id`、用户自然语言研究方向） ❌ |
| c | 换成错误密钥（对照） | **401** ✅ |

**结论**：AGENTS.md 写的加固步骤"对外暴露前设 `AUTH_DISABLED=0`"**堵不住这个洞**——只要前端 dev server 在服务用户，代理就会替攻击者完成认证。

**修复**：① 立即轮换为随机值，仅经环境变量/`runtime.env` 注入；② 把该字面量**加入** `_PUBLIC_INTERNAL_DEFAULTS`，让任何残留副本 fail-closed；③ 删除 7 处副本；④ `vite.config.ts` 去掉密钥兜底（缺失即报错），`host` 改 `127.0.0.1`。

---

### P0-3 硬编码的真实 DeepSeek API Key

**位置**：`scripts/wsl/start-dev.sh:31`

```bash
export DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-sk-1ae7************************c1a}
```

- 该值是**默认回退**，环境变量未设置时即生效；不是占位符格式。
- 它是挖掘链路的 LLM 入口，被实际读取：`backend/services/engine/alpha_agent/llm_client.py:148`、`backend/services/engine/rd_agent/llm_env.py:45`。
- 全仓库 `sk-[A-Za-z0-9]{20,}` 扫描**仅此一处**命中。
- **本目录不是 git 仓库**（无 `.git`），无法靠改历史清理 → 只能吊销。

**修复**：立即到 DeepSeek 控制台**吊销该 Key** 并换新；启动脚本已删除回退并改为只从服务端环境取 LLM 凭证。当前允许 `DEEPSEEK_API_KEY`、`AI_IDE_LLM_API_KEY`、`AI_IDE_API_KEY`、`OPENAI_API_KEY` 中任一项，且拒绝空值/占位符。**在吊销前不要把这个仓库交给任何外部人员。**

---

### P0-4 一个匿名请求即可让整个后端永久卡死并泄漏 30+ 进程（已实证）

**位置**：`alpha_factor_pipeline.py:436` 的 `async def` 处理器内**同步阻塞**调用 `compute_factor_via_qlib`（`:459`），全程无超时。

**实证**：

1. 发送一个 `promote-by-expression` 请求后，**`GET /health` 20 秒无任何响应** → 事件循环被占死，全站不可用。
2. 该进程收到 `SIGTERM` 后**不退出**（uvicorn 的优雅退出依赖事件循环，而它正被阻塞）→ 只能 `kill -9`；systemd/容器 `stop` 会超时。
3. `ps` 实测出现 **24 个**同命令行 python 进程，全部继承 8001 监听 socket（`fd=14`），导致端口在父进程被清理后仍无法释放。一次清理共 `kill -9` 了 **34 个**进程。
4. 该请求同时会把 qlib 的 joblib worker 全部拉起（无并发上限）。

**修复**：把重计算移出事件循环（`anyio.to_thread.run_sync` / 进程池 / 独立 job 服务），加请求超时与并发上限；给 uvicorn 加 `--timeout-keep-alive` 之外的处理级超时；限制 qlib `C.get_kernels` 的 worker 数。

---

## 三、P1 — 上线前必须处理

| # | 问题 | 证据 | 影响 / 修复 |
|---|---|---|---|
| P1-1 | **"生产形态" `run-wsl.sh` 根本不能用**：不导出 `INTERNAL_CALL_SECRET`/`SECRET_KEY` | `run-wsl.sh:1-46` | `expected=""` → 所有 `/api/v1/*` 401；`require_admin` 未被覆盖（覆盖只在 `AUTH_DISABLED` 下安装，`app_factor_mining.py:239`）→ 查不存在的 RBAC 表 → 403；且没有登录路由。结果：**dev 全开、prod 全关**，两种模式都不可用于真实上线。要么补上密钥注入路径，要么明确它是占位脚本。 |
| P1-2 | **JWT 签名密钥每次重启都重新生成** | 日志两次启动分别为 `db98ee28…`、`7d85539c…`；`config/runtime.env` 不存在 | 未落盘 → 每次重启所有令牌失效。`auth.py:76-88` 的注释正是警告这一事故。需持久化 `SECRET_KEY`。 |
| P1-3 | **逐用户 LLM Key 与用户研究内容曾写进全局可读日志** | 审查基线：`/data/alpha_agent_logs` 权限 `0755`、文件 `0644`；`grep -rl 'sk-'` 命中 **34 个文件**；577/1645 文件含用户 `direction` 文本；含 RD-Agent 的 `LITELLM_SETTINGS/**/*.pkl` | 当前根目录和一级任务目录为 `0700`；运行日志已脱敏。新增序列化工件扫描发现 5 个旧 `LITELLM_SETTINGS` 配置快照命中凭证字段（4 个命中常见密钥格式），已删除；剩余 1,242 个序列化文件未命中扫描覆盖的模式。任务文件不逐个 chmod 的风险由私有父目录隔离。 |
| P1-4 | **读取型 GET 会执行存量因子代码** | `alpha_factor_pipeline.py:426`（`GET /extractable`）→ `:82-93` 写临时文件后 `exec(...)` | GET 可被 CSRF，且每次翻页都重放；`limit` 无上限。应改为显式 POST 或纯 AST/正则解析。 |
| P1-5 | **任务终结写库失败 → 任务永远 "running"** | `launcher.py:1346-1362`（只 `logger.warning`）；`_db_cancel` 同（`:1364-1372`） | 一次 DB 抖动即让 UI/历史与事实长期不一致，只能靠下次进程启动的 `reconcile_orphans` 修。需有限重试 + 定期清扫过期行。 |
| P1-6 | **重启后孤儿子进程不被回收** | `launcher.py:1791` `start_new_session=True` | 重启后 `_load_tasks` 把任务标 FAILED（`:1466-1468`），但子进程继续烧 token、继续写因子、还会触发 materialize/validate（`run_rd_agent.py:696-699`）→ 为一个"失败"任务付费并产出数据。需持久化 pid/pgid 并在 reconcile 时 `killpg`。 |
| P1-7 | **`reconcile_orphans()` 全局无主** | `task_store.py:670-697`；调用方 `app_factor_mining.py:84`、`engine/main.py:128`（均无 `user_id`） | 语句是 `UPDATE ... WHERE status IN ('pending','running')`，前提"本进程此刻没有活任务"只在单进程成立；多实例/多 worker 会把别的实例的活任务刷成 failed。需加 owner/lease + 心跳。 |
| P1-8 | **零备份/零恢复** | 全仓库 `pg_dump\|pg_restore\|pg_basebackup\|backup` 无命中；无 cron/timer | PG 仅 **11 MB**，`pg_dump` 近乎零成本却完全缺失；111 GB QuantDB 也无副本。需夜间 dump + 异机副本 + 恢复演练。 |
| P1-9 | **无进程守护、无健康探针** | 无 systemd unit；`/health` 只被人手脚本消费（`verify-all.ps1:17` 等） | WSL/Windows 重启后 PG 自启（systemd）而应用不自启、无告警。需 unit + `Restart=always` + 探针。 |
| P1-10 | **`passlib 1.7.4` + `bcrypt 5.0.0` 组合已损坏** | 实测 `CryptContext(schemes=["bcrypt"]).hash("x")` → `ValueError: password cannot be longer than 72 bytes`（内部 `bcrypt.__about__` 缺失） | `auth.py:200,212`（`hash_password`/`verify_password`）与 `api_key_service.py:40,43` 是死代码。当前潜伏（无登录路由），一旦启用认证必炸。改 pin `bcrypt==4.0.1` 或直接调 `bcrypt`（`security.py:30` 已是这么做的）。 |
| P1-11 | **依赖完全未锁定，Python 版本约束无人强制** | `requirements-factor-mining.txt` 除 `pyqlib==0.9.7` 外全是 `>=`，无 lock、无 hash；实测已漂到 `pandas 3.0.6 / numpy 2.5.4 / starlette 1.8.0 / SQLAlchemy 2.1.4`；`pyproject.toml:10` 写 `>=3.10`，而 requirements 头注释写"Python 3.14"、正文又要求 3.12 | 无法复现构建；一次 `pip install` 可能拉进破坏性大版本。需 `uv`/`pip-tools` 锁版本 + hash，并在启动脚本里硬校验 3.12。 |
| P1-12 | **前端生产构建 bake 了空 env，认证开关失效且无登录路由** | `npm run build` 走 production 模式，**不读** `.env.development.local`；实测产物 `import.meta.env` 为空对象、`VITE_DISABLE_AUTH` 从未成为字面量 | 生产构建下 `disableAuth=false`、`getAccessToken()` 返回 null → 请求**不带任何 Authorization**，而应用没有 `/login`。结论：**当前前端只能配 `AUTH_DISABLED=1` 运行**。必须显式决定形态并落地（要么提交 `.env.production` 并接受"故意无认证"，要么补登录路由 + 挂 `authSlice`）。 |
| P1-13 | **缺生产部署形态**：`/backend-health` 只在 vite dev proxy 里有规则；`run-wsl.sh` 不服务 `web/dist` | `src/workspace/api.ts:46`；`vite.config.ts:29-33`；仓库无 nginx 配置 | 生产下顶部连接状态芯片会永久显示"服务连接异常"（即使后端健康）；且没有"前端产物 + 反代"的部署路径。 |
| P1-14 | **镜像与上游 `next` 的偏离远超文档** | 审查基线曾记录 **20+ 文件 differ**。2026-10-18 初次按本地文件 Git blob 与 `next` 树比对时为 72 个共同差异；后续修复后重建清单，当前为 **74 个共同文件字节不同，其中 23 个仅换行不同、51 个内容不同；另有 72 个镜像独有文件**；比较范围内 1,797 个上游文件未纳入此精简切片。还发现并修正了 85 个 `docs/` 文件名的 Windows 解包编码损坏（内容与上游 blob 完全一致）。 | 当前清单见 `MIRROR_DIFFS.md`。仍需逐项判断 51 个内容差异和 72 个镜像独有文件是否为有意本地修改；不得盲目从 `next` 覆盖。 |

---

## 四、P2 — 建议在上线前或上线后尽快处理

| # | 问题 | 证据 |
|---|---|---|
| P2-1 | 物料化中途崩溃会**静默混入新旧列值**：崩溃前部分分区是新值、部分是旧值，列名相同 → 无 schema 冲突、无报错；`describe()` 只用 3 个采样文件推 schema；`stats["errors"]>0` 仍会 register | `rd_mined_materialize.py:1796-1798,1898-1906`；`feature_source.py:255-258`；`quantdb_factor_reader.py:472-474` |
| P2-2 | 因子执行子进程**继承整个服务环境**（DB 口令 + 所有 provider key） | `routers/alpha_agent.py:515-517`、`launcher.py:1783-1792`、`alpha_factor_pipeline.py:86-93` |
| P2-3 | 硬件闸门**名不副实且 dev 默认关闭**：只是探针不加锁；真实上限 global 4 / per-user 2 在 8 核 32G 上已超配 | `hw_lock.py:144-145`；`start-dev.sh:22`；`launcher.py:145-148` |
| P2-4 | 回测进程注册表以 `factor_id` 为键会丢句柄 → 取消静默失效 | `routers/alpha_agent.py:518,532`；`factor_backtest/router.py:121-142` |
| P2-5 | 输入无上限：`factor_ids` 无 `max_length`（IN 列表无界）、`limit` 无 `le=`、`factors` 无界且逐条喂给 P0-1 | `alpha_factor_pipeline.py:48-50,55,394` |
| P2-6 | Vite dev server 绑 `0.0.0.0` 且注入管理员头，目前**仅靠 Windows 防火墙挡着**（三档 profile 全 ON、`BlockInbound`，3000/8001/node 零规则；WSL2 为 NAT 模式、8001 relay 只绑 127.0.0.1）。一次"允许"点击即变成局域网管理员代理，且 dev server 会直接吐源码 | `vite.config.ts:27,43` |
| P2-7 | **服务日志保留上限未显式验证**：独立入口写 stderr，由 systemd 收集到 journald；通用 `setup_logging` 的 RotatingFileHandler 未用于此入口。任务日志目录曾达 **3.0 GB**（单文件最大 94 MB） | `app_factor_mining.py:35`；`backend/shared/logging_config.py:11,48,64`。任务文件现有 90 天终态 GC；本轮 `journalctl --disk-usage` 实测 872.2 MB，journald 未配置明确 `SystemMaxUse`/`MaxRetentionSec`。 |
| P2-8 | 无 metrics / tracing / 告警：`GET /metrics` → 404；`prometheus_client` 声明未用 | 实测；`requirements-factor-mining.txt:16` |
| P2-9 | `runtime_secrets.load_runtime_env()` 本身仍未被调用；现有私有凭证文件不完整，不能满足后端启动所需的 LLM 与数据库凭证 | `start-dev.sh` 经 `scripts/wsl/load-runtime-env.sh` 读取外置 `~/.config/autoalpha/runtime.env` 并导出进程变量，Python `get_secret()` 通过 `QM_RUNTIME_ENV_FILE` 读取同一文件；本轮在 WSL 实查父目录 0700、文件 0600，当前 loader 仅加载 `INTERNAL_CALL_SECRET` 与 `SECRET_KEY`，没有 LLM Key 或数据库连接凭证 |
| P2-10 | ops 脚本普遍 `set -uo pipefail` 而**无 `-e`** → 部分失败当成功 | `status.sh:3`、`pg-setup.sh:5`、`link-quantdb.sh:11`、`fix-data-owner.sh:3`、所有 `install-*.sh` |
| P2-11 | 破坏性命令无确认、无路径校验：`rm -rf /home/qusong/autoalpha-venv312`（删的正是生产 venv）、`chown -R`、`chmod 755 /data` | `setup-python312.sh:22`、`rebuild-venv.sh:6`、`install-rdagent.sh:36,50,69,87`、`fix-data-owner.sh:11-12` |
| P2-12 | 基线配置曾包含弱默认值；当前 JWT/数据库密码已改为环境变量占位且无弱回退，监控密码字段为空。PG 初始化密码现由 Bash 生成 SQL 字面量并通过 stdin 传给 `psql`，不再进入 Python 子进程 argv；连接自检仍短暂经 `PGPASSWORD` 环境变量传递 | `config/unified/shared/security.yaml`、`config/unified/services/ai-strategy.yaml`、`config/unified/environments/{development,production}.yaml`、`config/monitoring_config.json`、`scripts/wsl/pg-setup.sh` |
| P2-13 | 磁盘风险：PG + 所有写入都在 C: 上的 19.5 GB `ext4.vhdx` 内，C: 仅剩 95 GB；日志无轮转单调增长 | `C:\Users\quson\...\LocalState\ext4.vhdx`；`df` |
| P2-14 | 从 `web/` 跑 npm 会打**官方 registry**（无 `web/.npmrc`），而 AGENTS.md 自称此处只有 ~30 KB/s；`dev-web.cmd` 用 `npm install` 而非 `npm ci` | `web/.npmrc` 不存在；`dev-web.cmd:21` |
| P2-15 | `scripts/README.md` 描述的是**不存在的目录结构**（build/training/data/ops/pipeline/redis、`.venv`、macOS 路径） | `scripts/README.md:1-95` |
| P2-16 | `_extract_qlib_expression` 在 `:88` 用 `exec` 执行 `factor_code`，且日志 fd 交给 `Popen` 未 `async with` | `alpha_factor_pipeline.py:86-93`；`factor_report/service.py:616`；`factor_research/service.py:748,881` |

---

## 五、P3 — 代码/仓库卫生

- 前端 **69 个孤儿文件 / 16,335 LOC（占 88%）**，`tsc` 会全量类型检查它们；AGENTS.md 称 "155 files"（实测 `web/` 99 文件、`src/` 90 文件）与实际不符（`web/src/App.tsx:34-46` 可达图实测 21 文件 / 4,103 LOC）。
- **6 个死依赖**：`react-redux`、`@reduxjs/toolkit`、`clsx`、`tailwind-merge`、`remark-gfm`（后者**完全没被 import**）。
- **7 条 npm advisory 全在 tailwind/postcss 构建链**，而该链产物为空（唯一 `@tailwind` 指令在孤儿 `src/styles/global.css:3-5`）→ 删掉即可清零。
- `authService.ts:213,225` 未加 `import.meta.env.DEV` 守卫地打印整个请求/响应体（登录路径 = 明文口令 + token）；当前不可达但会随包发布。仓库里已有 redactor 实现（`userCenterService.ts:24-48`）却没被复用。
- 两个 ~600 KB chunk，无 `manualChunks`（`Chart-*.js` 612 KB、`index-*.js` 604 KB）。
- `ui.tsx:23` 可点击 `div` 无 `role`/`tabIndex`/键盘处理；`ui.tsx:150` `aria-label` 挂在无 `role` 的 `div` 上。
- `App.tsx:46` `path="*"` 静默重定向，无 404 页。
- `ui.tsx:85-97` 递归无界 JSON stringify。
- `index.html` 无 CSP / 安全响应头。
- `lucide-react ^0.390.0` 被 caret 冻结在 `0.x`（永远升不上去）。
- `index.html:16` / `main.tsx:10` 引用 Inter / Noto Sans SC 但从未加载（设计字体不会生效）。
- `vite.config.ts:20-23` 的 `define` 是死配置（定义 `process.env.*`，源码读 `import.meta.env.*`）。
- 混合解释器的 `__pycache__` 被带进仓库（`cpython-310/312/314.pyc`）。
- `docker/` 只剩 24 MB 字体，无 Dockerfile/compose；`config/postgres/*.conf`、`config/redis/*.conf` 是未使用的上游副本，DR 时误导。
- **本目录不是 git 仓库** → 无版本控制、无回滚、无 `.gitignore`，且密钥无法靠改历史清理。
- Node **v25.2.1 非 LTS**（生产应用 22 LTS）。

---

## 六、已核实正常（不要在这些地方浪费返工时间）

- 后端 `/health`：**9 组 / 111 条路由全部挂载、`failed` 为空、6 步启动 DDL 全 ok**（与 AGENTS.md 一致）。
- 所有引擎路由都在 `/api/v1/` 下，被认证中间件统一覆盖，**无裸路由**。
- **SQL 全部使用绑定参数**；审查到的 f-string 拼接只涉及模块常量（状态集合、硬编码列名、占位符）。
- **任务幂等由 DB 强制**：`(user_id, fingerprint) WHERE ...` 部分唯一索引，`IntegrityError` 按约束名区分，失败方撤回内存占位。
- **路径穿越防护到位**：`factor_research/store.py:56-73` fail-closed；`quantdb_factor_reader.validate_source` 有 `_IDENTIFIER` 与排除表；`dataset` 是 `Literal`。
- **分页普遍有硬上限**；`resolve_history_filters` 夹到 200。
- **DB 会话管理正确**：`get_session` 在 try/except/finally 中提交/回滚/关闭，调用方均 `async with`。
- 启动命令构造是 **argv 列表，无 `shell=True`**；`task_id` 服务端生成（`uuid4().hex[:16]`）。
- 物料化 spawn 有白名单正则 + `O_NOFOLLOW` + `.tmp`→`os.replace` + flock。
- **CORS 非通配**：默认只放 localhost:3000/3001/5173。
- **JWT 路径 fail-closed**：`_require_secret` 拒绝公开字面量（`auth.py:123-135`）。
- 前端 `typecheck` **0 错误**、`build` 通过（3797 模块 / 19.13s，仅 1 条 chunk 体积警告）；**无任何 XSS 汇聚点**；**产物中无密钥**（内部密钥/`X-Internal-Call`/`X-User-Id` 均 0 次命中）；无硬编码非 localhost URL；lockfile `integrity` 398/398 且全部 npmmirror；错误处理与轮询清理都健壮（30s/10s/120s 超时、可读错误文案、重试按钮、错误边界、`abort()`+`clearTimeout`）。
- 三个 `scripts/*.ps1` **均为纯 ASCII**（符合 AGENTS.md 的 PowerShell 5.1 约束）。
- PG 只绑 `127.0.0.1`；WSL2 为 NAT 模式、8001 relay 只绑 loopback、Windows 防火墙 `BlockInbound` → **当前 8001 不可从局域网直连**（P2-6 是"一次放行即失守"的风险，不是既成事实）。
- 数据分布合理：111 GB QuantDB 在 E:（**独立物理盘**），与 C: 上的 vhdx 分离，单盘故障不会同时带走两者。
- 挖掘任务事后可诊断：`/data/alpha_agent_logs/<hash>/Loop_N/` 保留了会话轨迹。

---

## 七、未能验证（诚实标注）

1. 未做完整挖掘端到端跑（会改状态、耗时长）。
2. 未验证 `pg_hba.conf` 与 PG 角色实际权限（无免密 sudo，文件不可读）。
3. 未执行 `pip install` 复现依赖（会改状态）；版本漂移是**基于现有 venv 实测**，非全新安装结果。
4. **未验证该 DeepSeek Key 是否仍有效**（未对外发请求）——但无论有效与否都必须轮换。
5. P0-1 的 **HTTP 层干净复现**：HTTP 路径已取得"eval 确实执行"的痕迹（0 字节文件 + 每 worker 一次的 shell 报错），干净的内容型证明是在**直接调用同一函数**时取得的。未在 HTTP 层同时拿到 200 + 内容，因为该请求本身会把服务卡死（即 P0-4）。
6. 未验证 `_running_backtests` 是否覆盖 batch/matrix/cancel 全部入口（P2-4）。
7. 未重启 WSL/PG 实测"PG 自启、应用不自启"（按 `systemctl is-enabled` 与缺少 unit 推断）。
8. 未实测 `E:\quantdb` 字节数（9p 上 `du` 2 分钟未完成）。

---

## 八、上线前检查清单

**必须先做（P0，任一未完成都不得对外暴露）**

- [ ] 到 DeepSeek 控制台**吊销并轮换**旧 Key；仓库回退值已删除，启动脚本缺值即拒绝。
- [ ] 补齐现有 `~/.config/autoalpha/runtime.env`：本轮已确认父目录 0700、文件 0600，当前仅有内部/JWT 密钥；还需配置已轮换的服务端 LLM Key 与 `DB_PASSWORD` 或 `DATABASE_URL`。可在 WSL 以服务用户运行 `scripts/wsl/init-runtime-env.sh --complete`，交互输入隐藏并保留现有密钥。
- [x] 旧 `INTERNAL_CALL_SECRET` 已加入 `_PUBLIC_INTERNAL_DEFAULTS`；运行脚本和 Vite 配置不再包含可用副本。实际部署仍需自行生成随机值。
- [x] `web/vite.config.ts` 默认绑定 `127.0.0.1`，移除了密钥兜底；未提供密钥时不代注入身份头。
- [x] `config/` 中 JWT/数据库密码不再使用历史弱默认值；敏感配置改为环境变量占位或空值，PostgreSQL 初始化脚本不再把密码放进 Python 子进程 argv。
- [x] 请求表达式经过语法白名单并在受限子进程中执行；GET 提取分支不再执行存量代码。
- [x] `promote*` 重计算移出事件循环，设置超时、并发上限，并限制 qlib worker。

**上线前应做（P1）**

- [x] 认证形态已确定为仅本机可信开发环境免登录；不支持对外或生产暴露。若要对外服务仍需登录/可信认证网关。
- [ ] `run-wsl.sh` 尚无用户登录路由，需由独立可信认证网关保护后才能作为生产入口。
- [x] 私有 runtime.env 已有非空 `SECRET_KEY`（文件 0600）；当前免登录部署不使用 JWT。若启用认证或生产部署，仍需按届时密钥策略做运行态验证。
- [x] LLM、Embedding、MinerU 凭证仅从服务端环境解析；旧 MinerU Redis 读取与原始 Profile 凭证网关已移除/禁用，历史用户密钥来源的解析任务明确失败。
- [x] 任务日志目录/新文件权限已收紧；ASGI 日志处理器、HTTP 异常、任务/文档历史、直接错误响应与任务日志 API 已加环境密钥脱敏。
- [x] 当前 WSL 日志根及所有一级任务目录权限已实际修正为 0700。
- [x] `subprocess_stdout.log` 通过有界流式拷贝写入，并脱敏父/子进程环境中的凭证。
- [x] runner 退出后对 RD-Agent `common_logs.log` 做流式密钥脱敏并原子替换。
- [x] 扫描 RD-Agent 序列化工件中的凭证字段和常见密钥格式；删除 5 个命中的旧 `LITELLM_SETTINGS` 配置快照，并在 runner 退出或启动时处理遗留 runner 后清除新生成的同类快照。扫描未反序列化文件，不保证发现未知格式的密钥。
- [ ] 接入服务日志轮转；当前独立入口写 stderr 并由 systemd 收集到 journald，需明确并验证 journald 保留上限。
- [x] `GET /extractable` 已去掉 `exec` 分支，并限制返回数量。
- [x] 挖掘任务表增加进程 owner 与 180 秒租约；当前进程心跳续租，启动及每 30 秒回收过期活动任务；终态写入限定 owner 和活动状态。
- [ ] 执行任务租约数据库迁移并验证 queued 接管、重复启动 fencing 和过期回收路径。
- [x] 活跃任务状态持久化 runner PID/PGID、启动标识；重启时只终止身份仍匹配的遗留进程组。
- [ ] 配置独立 POSIX 备份目标并启用 `autoalpha-pg-backup.timer`；执行一次备份和 scratch DB 恢复演练。脚本/服务/计时器模板已在 `scripts/wsl/pg-backup.sh`、`scripts/wsl/pg-restore-check.sh`、`scripts/systemd/`；QuantDB 副本仍未实现。
- [x] 新增 systemd 后端 `Restart=always` unit、`/health` timer 和 journal 告警模板。
- [ ] 安装/启用并运行态验证 systemd unit、探针及失败告警；当前只写 journald，不接外部通知。
- [x] `bcrypt==4.0.1` 已锁定，兼容 passlib 1.7.4。
- [x] `requirements-factor-mining.lock` 已生成（Linux x86_64 / Python 3.12，SHA256 hashes）；安装入口使用 `--require-hashes`，启动脚本硬校验 Python 3.12。
- [ ] 补生产部署形态（反代配置 + 服务 `web/dist` + `/backend-health` 规则）
- [x] 重建"本镜像 vs 上游 `next`"的 blob 差异清单，修复 85 个文档文件名编码错乱，并更新 AGENTS.md（P1-14；可用 `scripts/mirror-diff-inventory.py` 重建清单）
- [ ] 逐项标注 51 个内容差异和 72 个镜像独有文件的来源/意图，再决定是否需要从 `next` 重新同步（禁止盲目覆盖）

**上线后尽快（P2/P3）**

- [x] 挖掘任务终态日志按 90 天留存清理，启动与每日 GC；目录权限保持私有。
- [ ] 为 journald 选择合适的存储/保留上限并验证；当前占用 872.2 MB，未发现显式 `SystemMaxUse`/`MaxRetentionSec`。独立后端通过 systemd journald 收集 stderr，任务日志另有 90 天终态 GC。
- [ ] `/metrics` + 告警；C: 剩余空间告警
- [ ] 物料化原子发布（先标 not-ready，成功后再 register）
- [x] 挖掘 runner 与因子回测子进程按需裁剪密钥环境变量；凭证名归一化额外覆盖 `TENCENT_SECRET_ID`、`AWS_ACCESS_KEY_ID` 与 SDK 的 `SecretId` 别名。
- [ ] 清理孤儿文件/死依赖/死 CSS 链（可同时清零 7 条 advisory）
- [x] 已新增 `web/.npmrc`，dev 启动使用 `npm ci`；`scripts/README.md` 已按真实结构重写。
- [x] 本地 Git 仓库已建立初始基线；`.gitignore` 排除本地环境密钥、虚拟环境、依赖、构建产物、日志与编辑器状态，保留环境变量示例。

---

## 附：关键证据的复现方式

```bash
# P0-2：公开密钥即管理员凭据（后端需以 AUTH_DISABLED=0 启动）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8002/api/v1/alpha-agent/tasks/history          # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Internal-Call: autoalpha-dev-internal-secret-9f3a' \
     -H 'X-User-Id: 10000001' http://127.0.0.1:8002/api/v1/alpha-agent/tasks/history                     # 200

# P0-1：门禁只检查是否含括号
python -c "print(all(c in '__import__(\"os\")' for c in ['(']))"   # True -> 通过 _is_qlib_expression

# P0-1：qlib 的 eval 汇聚点
sed -n '397p' /home/qusong/autoalpha-venv312/lib/python3.12/site-packages/qlib/data/data.py
#   -> expression = eval(parse_field(field))

# P0-1：空白被剥离（这解释了"看起来打不通"）
sed -n '502p;539p' /home/qusong/autoalpha-venv312/lib/python3.12/site-packages/qlib/data/cache.py
#   -> field = remove_fields_space(field)
```

---

*审查基线来自只读审查 + 受控实证。之后已对镜像文件做了部分修复，见“修复推进记录”；本报告中带有“审查时”的环境记录与证据仍作为历史记录保留。修复后只完成静态语法/配置检查，尚未重跑完整运行态验收。*
