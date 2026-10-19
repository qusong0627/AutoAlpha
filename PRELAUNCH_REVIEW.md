# AutoAlpha 因子挖掘模块 — 上线前全面审查报告

- **审查对象**：`D:\git\autoalpha`（`quantmindoss@next` 因子挖掘切片的独立部署镜像）
- **审查时间**：2026-10-18 19:56 – 20:15（Asia/Shanghai）
- **审查方式**：静态代码审查 + 运行时实证（启动 PG/后端/前端、发真实 HTTP 请求、受控 PoC）
- **结论**：**当前状态不可对外上线。** 审查时确认的 4 项 P0 中，P0-1（表达式 RCE）、P0-2（公开内部密钥）和 P0-4（事件循环阻塞）已有代码修复；本机当前后端已运行并由 systemd 管理。P0-3 的旧 DeepSeek Key 是否已在供应商侧吊销仍未确认。
  若严格限定为"仅本机开发"，也**必须先确认旧 DeepSeek Key 已吊销并配置新 Key**；仓库代码和本地环境变量无法证明供应商侧凭证已失效。

> **环境范围**：此处 WSL/systemd、磁盘和本机服务状态只记录临时验证环境的实测结果，不是产品必须采用的长期部署形态或本轮代码验收要求。当前产品目标仍是本机单人开发、免登录；QuantDB 继续使用现有软链接，本轮不复制数据。

## 修复推进记录（2026-10-18）

本报告下方的发现是审查时的基线，不应再被当作当前代码状态。后续已在镜像内落地部分修复；本机后端与服务守护已运行态验证，Vite 本地代理已做基本验证，完整 UI 与挖掘端到端仍未验收。

- 2026-10-19 本轮推进：按上游 `next` 重建差异清单并将内容差异补至 58/58 分类（80 个共同文件差异：22 个仅换行、58 个内容差异；87 个镜像独有文件）。任务租约验证通过：过期 queued 接管、owner fencing、并发启动仅一个成功、孤儿终结并清租约；四条随机临时任务均在 `finally` 删除并复核不存在，未启动真实挖掘。WSL 当前仅作临时验证环境，后端健康、9 组路由无失败，monitor/backup timers active；本机 journal 有健康探针、磁盘检查、PG 备份成功记录。bwrap 在 WSL 内可运行只读根/隔离命名空间冒烟。Vite 按仓库脚本临时启动后，页面模块及 API proxy 均 HTTP 200，免登录 smoke 通过，验证后已停止且 3000 端口已释放。独立入口新增请求 ID 校验和 access log middleware，目标 Python 3.12 语法编译通过；未重启当前 WSL 后端，因此该代码尚未加载到运行进程。没有跑完整页面交互、真实挖掘、回测 worker 故障或告警触发注入。
- 本轮范围约束：旧 API Key 吊销按用户要求暂不处理；QuantDB 备份/复制继续搁置并保留现有软链接。告警渠道按用户选择为本机日志，无需外部通知服务。
- 随后整理 WSL 脚本：将 24 个一次性探测、旧认证冒烟、任务特定排查、绕过锁文件的单包安装器，以及会启动真实 `/evolve` 任务的旧 DB 冒烟移入 `scripts/wsl/archive/`；新增归档说明，并从常规验证清单移除有真实任务副作用的 DB 冒烟。保留锁定依赖安装、数据链接/缓存、PostgreSQL/监控部署、只读状态检查与任务租约冒烟等可重建或日常入口。镜像独有文件更新为 88 个。
- 本轮上传入口资源边界补强：文档上传在调用 Starlette multipart parser 时将 ``max_files`` 限为 20、``max_fields`` 设为 0，避免解析器先接收大量文件/普通字段后才由 handler 检查数量；请求处理结束和复制到私有目录后均显式关闭表单临时文件，不依赖 GC。保留 handler 级文件数校验用于稳定业务错误。`py_compile`（本机 Python 3.10）和目标文件 `git diff --check` 通过；本机没有目标 Python 3.12，未做运行态 multipart 验证。
- 本轮免登录启动护栏补强：`AUTH_DISABLED=1` 仅允许 `development`/`dev`/`local`/`test`/`testing` 环境，未知值和常见生产别名（如 `prod`）一律拒绝启动，避免只拦截 `production`/`staging` 字面值造成环境别名绕过。静态语法检查完成；启动矩阵未做运行态验证。
- 本轮免登录依赖安装失败改为 fail-closed：`AUTH_DISABLED=1` 下若 FastAPI 的 `get_current_user` / `require_admin` 覆盖无法完整安装，应用现在抛出启动错误，不再以“中间件已免登录、admin 路由仍走原鉴权”的半配置状态提供服务。仅完成静态复核，故意破坏依赖导入的启动路径未运行态验证。
- 本轮任务租约补强：Qlib 数据准备、池检索及配置解析可能耗时，runner 启动前新增一次数据库 owner/活动状态心跳校验；若期间租约已被接管或任务已终结，先停止并退出，不启动新的因子代码子进程。静态语法检查完成；租约竞态尚未做运行态验证。

- 已修：表达式严格白名单及 GET 提取路径去除 `exec`；promote 重计算移出事件循环并加超时/并发上限；内部密钥默认值 fail-closed，Vite 秘密兜底移除且仅监听回环。
- 上述 P0 修复的当前证据：`alpha_factor_pipeline.py` 只接受无属性/下标/字符串的有限 Qlib token 与算子，并在 spawn 进程池中计算；并发数和硬超时受配置限制。2026-10-19 通过当前 Vite 代理向 `/promote-by-expression` 发送含 Python 调用语法的表达式，API 返回“不是合法的 Qlib 表达式”，随后后端 `/health` 仍 healthy；`/extractable` 读取了 6 条历史因子记录，均未标记为可提取，health 仍正常。历史记录中没有可用于验证恶意因子代码的样本；有效表达式推广及 P0-4 超时回收仍待运行态验证。`auth.py` 将已公开的内部密钥加入 fail-closed 黑名单；Vite 仅在显式提供服务端环境密钥时才注入内部身份头。
- 本轮启动校验修复：`start-dev.sh` 将必需凭证赋值与 `export` 分开，避免 Bash `export VAR=$(...)` 掩盖 `require_env` 的失败状态、导致缺凭证时仍继续启动；同时拒绝短于 32 字符、含空白或属于已公开默认模式的 `INTERNAL_CALL_SECRET`，并拒绝空白/占位 LLM Key。`DATABASE_URL` 路径也必须含非空密码，防止越过 `DB_PASSWORD` 的必填检查。私有 env 初始化脚本在隐藏输入时拒绝含空白的 LLM Key；错误信息不回显密钥。
- 本轮 P0-4 配置边界补强：`QLIB_KERNELS` 代码级最大 4、promotion 并发最大 4、超时仅接受有限正数且最大 1800 秒；避免错误环境值把 worker 数或等待预算放大到无界。超过上限会记录变量名并截断，不记录配置内容。
- 本轮 P0-4 池故障补强：ProcessPool 提交或执行阶段抛出 `BrokenExecutor` 时终止/清理旧池并返回 503，让下一次请求重新惰性建池；此前仅超时分支会重建进程池。尚未做 worker 崩溃运行态注入。
- 本轮 P0-4 收尾补强：超时/worker 异常后的 `join()` 与进程池关闭移至 `asyncio.to_thread`，不再阻塞 API 事件循环；重置按失败 executor 身份进行，只摘除旧池，不会误关并发请求刚创建的新池。Python 语法检查完成，运行态超时回收仍待验证。
- 本轮 P0-4 取消路径补强：HTTP 请求取消时不再立即释放 promotion semaphore；并发名额由底层 `ProcessPoolExecutor` future 的完成回调释放。独立 watchdog 在客户端断开后仍执行硬超时并重置对应旧池，防止取消请求绕过计算时限或无限堆入 worker 队列。Python 语法检查完成，取消/排队行为仍待运行态验证。
- 本轮密钥路径加固：Bash loader 和 Python `runtime_secrets` 现在逐层检查 runtime env 的祖先目录，拒绝符号链接、组/其他可写目录和非当前用户/root 所有的路径；初始化器也验证 `$HOME`、`~/.config` 的属主与写权限，降低私有密钥目录被替换的风险。
- 已修：任务日志私有权限、子进程环境裁剪、任务终结写库重试、回测请求上限、因子子进程 bubblewrap 隔离、遗留任务进程组身份记录。
- 本轮命名子进程收口：任务结束后由 API 进程异步读取/更新因子中文名，不再由密钥已裁剪的 RD-Agent/统计 runner 再启动一个 DB 子进程；保留的手动 `name_factor_batch.py` 入口安装日志脱敏。尚未做端到端命名验证。
- 本轮 RD-Agent 执行面收口：`compute_factor_ic` 执行生成因子的子进程改为 bubblewrap 隔离，关闭网络、只读挂载根与输入 H5、将 HOME/临时目录换成 tmpfs、只传最小运行环境，并遮蔽已知 runtime.env 路径；缺少 bwrap 时拒绝执行因子代码。尚未做运行态沙箱验收。
- 本轮补齐 runner/生成代码边界：RD-Agent 的 `qrun` / `read_exp_res.py` 子进程现在经 bubblewrap 禁网运行，移除 API Key、DB、JWT 与其他凭据环境变量；因子物化代码执行也改为 bubblewrap，只读挂载 panel/脚本、单独绑定结果目录，并在缺少 bwrap 时 fail-closed。RD-Agent runner 只写私有结果 JSON，API 进程在 runner 退出后查重和落库；统计派作为固定表达式算法单独保留 DB 环境。结果 JSON 使用 no-follow/大小/结构限制。尚未做 bwrap 运行态验收、完整挖掘及落库验收。
- 本轮执行路径补漏：复核所有挂载的 RD-Agent API 后发现轻量回测曾直接在 API 进程 `exec` 因子库代码；现复用 bubblewrap 禁网/只读根/最小环境策略，回测子进程仅获短时运行、单文件大小和进程数限制，父进程只解析有大小与类型校验的有限指标 JSON，并且不记录因子异常原文。缺少 bwrap 时拒绝执行；仅做静态语法检查，沙箱运行态未验证。
- 本轮 RD-Agent 轻量回测并发收敛：不同因子也共享单实例上限，避免并发请求无界拉起沙箱子进程；抢占名额在状态写库失败时回滚。仅完成静态审阅，运行态限流未验证。
- 本轮通用回测子进程收口：stdout/stderr 改为并发排空、各自只保留末尾 256 KiB，避免不可信因子用无限输出耗尽 API 内存；子进程独立会话启动，超时/取消按进程组终止，取消等待不阻塞事件循环；任一管道读取线程启动失败也会终止子进程并关闭管道。尚未做运行态取消/超时注入。
- 本轮失败工件读取收口：回测异常路径只从本次私有 workspace 内以 no-follow 打开普通 traceback 文件，并最多读取末尾 64 KiB；避免因子代码借助同名符号链接让父进程读取外部文件，或用大错误文件放大内存占用。仅完成静态检查。
- 本轮回测结果读取加固：父进程读取因子生成的 Parquet/HDF 前先用 `lstat` 拒绝符号链接、非普通文件、越出本次 workspace 的路径和超过 512 MiB 的文件，防止将因子输出路径直接交给 pandas 跟随链接读取。尚未做格式畸形样本验证。
- 已修：Python 3.12 启动检查、bcrypt 兼容版本、Python 哈希锁、前端 npm 锁安装与 registry、关键运维脚本 fail-fast/破坏性操作保护。
- 本轮补充：免登录模式校验 loopback `Host`，并拒绝写请求的非本机/opaque `Origin` 与标记为 cross-site 的无 Origin 请求，降低浏览器 DNS rebinding/跨站请求风险；`ENVIRONMENT=production|staging` 时拒绝以 `AUTH_DISABLED=1` 启动。这些措施不认证本机进程，也不替代网络边界。
- 本轮免登录边界复核：`Host` 解析现在也读取并验证端口，拒绝非数字、越界及空端口形式，避免 `urlsplit().hostname` 单独解析时把畸形 authority 当作 localhost 接受。尚未做 HTTP 层运行态探针。
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
- 本轮文档修正：清理方向拆解、文档整理、因子代码生成和 LLM 客户端中“用户 Profile Key 优先”的过时说明，明确当前挖掘部署仅使用服务端环境变量。2026-10-19 重建镜像清单后，共同文件差异为 80 个（22 个仅换行、58 个内容差异），镜像独有文件 87 个；上游工作树状态为空。补分类 RD-Agent 生成代码的 bubblewrap 隔离执行、轻量回测的输入校验/单并发/子进程隔离，以及请求 ID 的日志安全校验。
- 本轮数据库迁移核验：尝试以 `quantmind` 做只读 schema 查询，因本机 PostgreSQL peer 认证拒绝而未能查看列/索引；非交互 `sudo -u postgres` 也因需交互认证失败。未对数据库做任何修改，backend systemd unit 当前 inactive，因此租约迁移与运行态行为仍待有权限的数据库会话验证。
- 本轮环境与版本控制核验：本地 `web/.env.development.local` 只有 `VITE_*` 地址/模式/免登录开关等变量名，没有 API 凭证变量，且已由 `.gitignore` 排除；环境示例仍可跟踪。原本已初始化但 0 文件、无 HEAD 的本地仓库现建立基线提交 `9c0bd3b`（1,316 文件）；没有 Git remote。已对提交内容做常见密钥格式扫描，未命中。PostgreSQL TCP 密码认证也因未配置可用密码而拒绝，迁移未执行。
- 本轮网络运行态复核：WSL Ubuntu 是 WSL2，未配置用户 `.wslconfig` 或 `netsh portproxy`；Windows 侧 8001 仅观测到 `127.0.0.1` 的 `wslrelay` 监听。WSL 内仍有手动启动的 Uvicorn（PID 1557，监听 `0.0.0.0:8001`），systemd unit 显示 inactive；`GET /health` 返回 healthy，`auth_disabled=true`、`startup_ddl=true`、9 组/111 条路由已挂载。Windows 防火墙各 profile 已启用，但入站动作显示 NotConfigured；loopback 监听和无 portproxy 不能证明所有 WSL/容器/网络路径均不可达。未终止这个非本轮启动的后端进程。
- 本轮私有环境补全未完成：已尝试通过 Codex PTY 启动 `init-runtime-env.sh --complete`，但终端面板返回 queued、当前可见终端未切换；脚本停在 LLM 变量名提示时已中止并释放锁，没有输入密钥或写文件。复查 `runtime.env` 仍为 `0600`、属主 `qusong`，只含非空 `INTERNAL_CALL_SECRET` 与 `SECRET_KEY`；无临时文件。需在可见的 WSL 终端完成隐藏输入，再重启手动启动的后端以加载新环境。
- 本轮启动路径修复：`start-dev.sh` 与 `load-runtime-env.sh` 使用了 Bash 不支持的 `${!name:-}` 间接参数默认展开，会在读取变量时触发 `invalid indirect expansion`。已改为按变量名调用 `printenv`；环境加载器与补全脚本还会拒绝 CR 行尾，避免 Windows CRLF 混入凭证。用当前受保护的 runtime.env 实际加载后仅确认内部/JWT 两个变量，不输出值。
- 本轮 P2-1 修复：`rd_mined` 物化和 CUSTOM→CN promotion 共用进程锁；写入前创建 `.materialization-incomplete` 标记，异常/崩溃时保留标记并阻止全量恢复成功前读取。reader、因子报告目录入口、因子研究自动发现均识别该标记；失败批次不注册，promotion 的残留标记要求 `--force` 全量重拷。仅完成代码静态检查，尚未做崩溃注入或运行态验证。
- 本轮代码复核补强：promotion 分区枚举或 `stat` 遇错、`dt=*` 目录缺少 `data.parquet` 时不再发布部分列表；根目录和分区文件均按不跟随链接的方式检查，扫描异常整体中止，避免 `--prune` 把不完整/不可读/瞬时消失的源分区误判为已删除。尚未做运行态验证。
- 本轮数据路径加固：`promote_rd_mined.py` 拒绝符号链接数据集根、分区目录和 parquet 文件；拷贝时用 `O_NOFOLLOW` 打开普通源文件、同目录独占随机临时文件、fsync 后原子替换，写入/清理期间发现分区链接即记录失败并保留不完整标记。没有更改部署所需的 `/data/quantdb` 根软链接。静态语法检查完成，尚未在实际 QuantDB 上运行毕业流程。
- 本轮 P2-3 缓解：单机部署的挖掘并发默认值从全局 4 / 每用户 2 收紧为全局 1 / 每用户 1，避免 dev 绕过最低硬件门槛时并行启动多份 RD-Agent。环境变量仍可显式提高；容量统计是进程内的，暂不支持把多 Uvicorn worker/多实例视为共享的全局限额。
- 本轮 P2-4 修复：回测子进程句柄现在按 `run_id` 登记，`factor_id` 只映射当前运行；旧运行延迟退出/清理时按句柄身份检查，不会删除新运行的句柄。取消端点按当前 `run_id` 精确取进程，旧式调用保留兼容回退。正常退出、取消和超时均会清理子进程组（包括主进程已退出但仍持有管道的后代）；函数式、Factor 类和表达式回测的私有工作目录由统一 `finally` 清理，并恢复后代创建的不可访问目录权限；表达式结果在父进程读取前校验为工作区内的普通文件。仅完成静态检查，竞态、取消及清理行为未做运行态注入验证。
- 本轮运维脚本修复：`status.sh` 保留全项巡检，每个数据库/健康检查失败都会明确报告并最终返回非零；现与备份/恢复脚本一致，支持从 `DATABASE_URL` 解析 PostgreSQL 主机、端口、用户、库名、密码及 TLS 选项，连接子进程不继承完整 URL。`fix-data-owner.sh` 递归目标经 `realpath` 限定在 `/data` 下、chown 不跟随符号链接，并将根目录改为 0711 以隐藏目录列表。危险的 venv 替换已有交互确认，属主脚本默认预览。仅完成静态审阅。
- 本轮 P2-7：journald drop-in 已安装到当前 WSL 的 `/etc/systemd/journald.conf.d/60-autoalpha.conf` 并重启服务；`systemd-analyze cat-config` 确认 2 GiB / 保留 5 GiB 空间 / volatile 512 MiB / 30 天均为有效配置，服务为 active。现有 journal 用量 872.2 MB；未 vacuum 旧日志。该设置作用于整套 WSL journald。
- 本轮 P2-13：新增只读 C: 空间检查与 6 小时 systemd timer，低于 20 GiB 时经 OnFailure 向 journal 写 critical 告警；当前 timer active，首次运行成功，C: 可用 95 GiB。WSL `/` 的虚拟可用量约 1 TiB，不能替代宿主机卷监控；低空间告警触发路径尚未做故障注入。
- 本轮 P1-9 部署：每分钟 `/health` 探针与 journal critical 告警已启用；`autoalpha-backend.service` 已安装并启用。运行态确认没有 queued/running 挖掘任务后，将私有 runtime.env 补齐数据库 URL 与服务端 LLM Key，并将手动 Uvicorn 切换到 systemd。受控终止主进程后，systemd 自动重启为新 PID；`/health` 返回 healthy、免登录开启、9 组路由、`failed={}`。
- 本轮备份工具修复：`pg-backup.sh` 和 `pg-restore-check.sh` 从 `DATABASE_URL` 解析 host/port/user/password/database 及常用 TLS 选项；备份仅允许本机 PostgreSQL，使用 `pg_lsclusters` 匹配在线集群而不要求应用角色读取受限的 `data_directory` 参数。DrvFS 目标必须通过 Windows ACL 校验；连接前从子进程环境移除完整 URL 与重复 DB_PASSWORD。
- 本轮补充：PostgreSQL custom-format 备份、SHA256 校验、14 天保留清理、夜间 systemd timer 和失败 journal 告警已部署。E:\AutoAlphaBackups 位于与 PGDATA 不同的 E: 物理盘；其 Windows DACL 禁止继承并只授权当前账户、SYSTEM、Administrators，备份脚本每次运行前验证。手动备份和 systemd 备份均成功，checksum 通过；scratch DB `autoalpha_restore_20261019` 恢复出 16 张 public 表。E: 故障及 QuantDB 副本仍未覆盖。
- 本轮数据链接加固：`/data/quantdb` 继续指向 `/mnt/e/quantdb`；建链脚本现在确认源位于实际挂载的 `/mnt/e`，保留并验证正确的既有链接，对错误/断开的链接及被实体文件/目录占用的目标 fail-fast，不再先删除再重建。`/data` 下的写入根目录拒绝符号链接，避免 `mkdir` 意外写入链接目标。2026-10-19 在现有 WSL 上重跑成功，样本 parquet 可读；QuantDB 独立副本仍按用户决定暂缓。
- 本轮免登录网络收窄：systemd 开发服务从 `0.0.0.0` 改为在 WSL 内只监听 `127.0.0.1:8001`；`start-dev.sh` 默认 loopback，并在 `AUTH_DISABLED` 时拒绝非 loopback 的 HOST。2026-10-19 再次确认无活动任务后重启并加载该启动护栏，新 PID 77784；`/health` healthy，Windows localhost 可达，WSL 私网 IP 的 8001 连接被拒。生产形态 `run-wsl.sh` 保持 auth ON，未改。
- 本轮 Vite 代理验证：启动镜像自己的 `scripts/dev-web.cmd`，Vite 只监听 Windows `127.0.0.1:3000`；`/backend-health` 经代理返回 healthy，`/api/v1/alpha-agent/tasks/history?limit=1` 返回 200。Vite 未设置内部密钥，不注入身份头；开发后端按预期免登录。后端重启后已复验代理仍正常。完整页面交互和挖掘提交流程未验证。
- 本轮凭证入口收口：移除遗留 `docMiningApi` 的 MinerU 用户密钥 payload/保存方法、`userCenterService` 的 LLM Key 读写/测试方法，以及无引用的 Embedding 用户密钥表单 payload 助手；MinerU 状态类型只表达部署环境来源，历史设置清理保留为删除接口。MinerU 缺配置提示统一指向受保护的部署环境，不再指向已禁用的用户设置页。旧方法和表单助手均无调用点；本轮最终状态下 `npm.cmd run typecheck` 退出码 0，两个改动过的 Python 模块 `py_compile` 通过。
- 本轮运行时链接加固：RD-Agent 的 Qlib 链接比较改用解析后的绝对目标；已有实体目录改链时使用不覆盖的唯一备份名，备份名冲突则保留原目录，创建链接失败会恢复旧目录/旧链接。移除了旧逻辑对固定 `_backup` 目录的递归删除。此项是镜像本地改动，后续上游差异清单需纳入核对；Python 语法检查通过。2026-10-19 01:12 在无活动挖掘任务时重启 systemd 后端，确认新 PID 74445、`/health` healthy、9 组路由无失败；后续为收窄监听又于 01:13 重启至 PID 75249，health 仍 healthy；01:22 加载启动护栏后重启至 PID 77784，health healthy。只读盘点确认 `/home/qusong/.qlib/qlib_data/cn_data` 正确解析到 `/data/qlib/cn_data`，既有 `cn_data_backup` 保留未动；真实挖掘任务尚未触发该路径。
- 本轮补充：挖掘任务表增加进程 owner 与租约截止时间；进度只续当前进程持有的活动任务，终态写入受 owner/活动状态约束，启动及每 30 秒回收过期租约。2026-10-19 已在本机 PostgreSQL 幂等增加 `owner_id`、`lease_expires_at` 和 `idx_rd_mining_tasks_lease`；运行态查询确认三项存在，且迁移前无活动任务。私有 env 补齐后已重启为 systemd 管理的新进程，租约代码已加载；实际 queued 接管、重复启动 fencing 和过期回收仍需单独运行态验证。
- 本轮租约终态清理：`reconcile_orphans()` 将过期活跃任务标记为 `failed` 时同时清空 `lease_expires_at`，保留 `owner_id` 作为审计信息。Python 语法编译通过；PID 85230 已加载该改动，但租约故障场景仍未注入。重启前最近 50 条任务有 0 条 pending/queued/running。
- 本轮补充：新增服务端密钥值与常见 Bearer/API-key 形式脱敏；ASGI 日志处理器、HTTP 异常、直接错误响应、任务状态/历史行、文档错误字段与 `subprocess_stdout.log` 写入/读取均经过脱敏。RD-Agent 自行创建的 `common_logs.log` 在 runner 退出后也会流式脱敏并原子替换；历史序列化工件已扫描清理，后续 runner 收尾会删除 `LITELLM_SETTINGS` 快照。任务目录仍以 0700 私有权限隔离。
- 本轮脱敏规则补强：日志脱敏从仅匹配 `_API_KEY/_TOKEN/_SECRET/_PASSWORD` 后缀改为归一化环境变量名，并识别 `APIKEY/AUTHORIZATION/TOKEN/SECRET/PASSWORD/ACCESSKEY/PRIVATEKEY/SIGNINGKEY/CREDENTIAL/COOKIE/SUBSCRIPTIONKEY`；键值对文本规则也覆盖带前缀及 JSON 引号包裹的 `AWS_ACCESS_KEY_ID`、`TENCENT_SECRET_ID`、`*_API_KEY` 等命名。`LLM_EXTRA_HEADERS` 和 `QMF_FACTOR_NAME_HEADERS` 的整段配置及解析出的头值也参与脱敏；本轮补充将 `DATABASE_URL`、`REDIS_URL`、PostgreSQL/Celery 连接串环境值整体纳入精确值脱敏，并识别对应赋值字段，避免只局部遮住密码。静态语法检查完成；脱敏样例未做运行验证。
- 本轮前端日志脱敏补强：用户中心通用 API client 不再记录请求/响应正文或 URL，只记方法和状态码，避免研究内容及 query 参数凭证进入浏览器控制台；错误摘要只保留方法、状态码和 Axios 错误码，不输出 URL/消息。前端 `npm.cmd run typecheck` 通过。
- 本轮工作台 API 错误日志收口：共用 Axios client 不再输出原始 URL、请求体、响应体或 Axios error 对象；只记 HTTP 方法/状态码/错误码，避免 Authorization、API Key query 或服务端错误内容进入浏览器控制台。`npm.cmd run typecheck` 通过。
- 本轮遗留策略客户端日志收口：`StrategyManagementService` 所有异常分支不再打印原始 Axios error，仅记录方法、状态码、错误码，防止配置/响应对象中的认证头与请求数据进入控制台。`npm.cmd run typecheck` 通过。
- 本轮免登录 CORS 补强：`AUTH_DISABLED=1` 时 FastAPI CORS allowlist 复用严格 loopback origin 集合，通配符配置回退到本机前端来源，不会允许任意网站读取 localhost API；加入 Vite preview 默认 `localhost/127.0.0.1:4173` 来源。Python 静态语法检查完成；浏览器跨站读取行为未运行验证。
- 本轮 Vite 代理边界补强：仅在 Vite 进程显式持有 `INTERNAL_CALL_SECRET` 时，为 dev 与 preview 的 `/api` 代理都安装来源门禁；拒绝非 loopback Host、跨站 Fetch Metadata、Origin 非同源或 Origin 与 Host 不一致的请求；缺少 Origin 的非安全方法也拒绝，防止恶意网页通过 localhost 代理诱导注入管理员身份头。此前遗漏的 preview hook 现复用同一门禁。`npm.cmd run typecheck` 与 `npm.cmd run build` 通过；构建只报告现有大 chunk 提示，尚未做浏览器 CSRF 运行验证。
- 本轮运行态修复：将现有 WSL `/data/alpha_agent_logs` 及全部一级任务目录从 0755 改为 0700；实查没有权限不符的一级任务目录。随后扫描删除了 5 个命中凭证标记的旧 `LITELLM_SETTINGS` pickle。
- 本轮补充：新增本机后端 `Restart=always` systemd unit、每分钟 `/health` 探针 timer 和 journal critical 失败记录模板；`/health` 现含限时 PostgreSQL `SELECT 1` 与核心启动 DDL 状态。健康探针 timer 已安装启用并对手动后端执行成功；后端守护 unit 已安装启用并通过受控主进程终止验证自动重启；health timer 持续监测，不代替数据库写入探测或外部通知。
- 本轮补充：独立入口接入任务日志 90 天留存 GC，启动和每日执行一次，只清终态超龄目录；清理前拒绝根/任务目录符号链接并确保目录权限 0700。服务 stderr 由 journald 收集；journal 全局上限现已安装并生效，初始用量 872.2 MB，未清理旧日志。
- 本轮依赖环境修复：实际 Python 3.12 venv 原装 `bcrypt 5.0.0`，现已用带哈希锁文件同步全部 217 个包；锁文件额外限制 `Pillow<13`、`websockets<17`、`importlib-metadata<9`，兼容同环境中的 Streamlit、Prefect、LangGraph SDK、LiteLLM。安装后逐包版本与锁一致，`pip check` 返回无依赖冲突；更新当时 Uvicorn 未重启，后续 PID 85230 于 01:48 重启加载更新后的环境，`/health` healthy。
- 当前未闭环：DeepSeek 旧 Key 的供应商侧吊销；QuantDB 的 E: 外部副本/异机备份；租约接管与过期回收故障路径；生产认证/反代（当前明确仅本机免登录）；完整挖掘端到端和告警故障注入验收。PostgreSQL 备份/恢复、后端自动重启及 Vite 到后端的基础代理请求已运行态验证。
- 当前认证决策：仅本机可信开发环境保持 `AUTH_DISABLED=1`；Vite 默认只监听 `127.0.0.1`。该设置不构成生产免认证方案。

## 当前部署决策补充（2026-10-18）

用户确认：API 凭证由服务端环境变量提供；当前阶段不启用用户登录认证。挖掘 LLM、Embedding、MinerU 的运行时解析现只读服务端环境；Profile/Redis 历史凭证不再参与挖掘，在线写入接口已禁用。`config/runtime.env.example` 是模板，实际密钥应放在 WSL ext4 的私有目录 `~/.config/autoalpha/runtime.env`（权限 `0600`），不要放在 D: 仓库目录、前端 `VITE_*` 变量或提交记录中。脚本已接入该文件并在缺少必填凭证时拒绝启动。

这项决策把部署范围限定为**单用户、本机开发**。`AUTH_DISABLED=1` 下所有 API 调用都拥有开发管理员身份，因此环境变量只能保护凭证的来源，不能隔离同机调用者或不同操作者；CORS 也不是认证或 CSRF 防护。按此范围，建议优先完成：

1. **吊销已泄漏的旧 DeepSeek Key 并换新**，随后确认新 Key 只存在于受保护的运行时环境；这是当前唯一不能靠代码改动替代的 P0。
2. **网络边界已收窄但无认证仍只限本机**：2026-10-19 确认 WSL NAT、没有 `netsh portproxy`、Windows 8001 listener 仅 `127.0.0.1`；Windows Firewall 三个 profile 的有效默认入站策略为 `BlockInbound`，无启用的 8001 入站 allow 规则。Windows 主机可直连 WSL 私网地址；未从独立局域网设备验证路由，不能宣称远程不可达。后端 loopback `Host` 与写请求 `Origin` 检查只约束浏览器请求，不认证同机进程。
3. **完成凭证生命周期**：实际生成并配置随机 `INTERNAL_CALL_SECRET`（它在认证关闭时仍是服务间凭证，开启认证时又可铸造管理员身份）；限制运行时文件权限；继续检查日志、异常和诊断输出是否会泄漏 Key。ASGI 日志、子进程 stdout、任务结束后的 RD-Agent `common_logs.log` 和 API 错误返回已加脱敏；序列化工件已按已知凭证模式扫描并删除 5 个旧设置快照，未知编码形式仍无法由字节特征扫描排除。
4. **补齐数据与进程恢复能力**：PostgreSQL 已夜间备份至 E: 并完成 scratch DB 恢复演练；E: 与 PGDATA 所在 ext4 是不同物理盘，但 E: 同时承载 QuantDB，因此 E: 盘故障会同时带走 QuantDB 和 PostgreSQL 备份。还缺 QuantDB 的独立副本或异机备份。挖掘任务已有进程 owner、180 秒租约与 30 秒周期清扫，数据库迁移已执行且新服务已加载；queued 接管、重复启动 fencing 和过期回收仍待运行态验证。
5. **完成本机运维可观测性**：后端 systemd 守护、`/health` 探针、journal 告警、日志上限、C: 磁盘告警和 PostgreSQL 备份告警已启用；服务自动重启、数据库备份及 scratch DB 恢复已验证。独立入口的 Prometheus `/metrics` 已加载并在运行态返回 HTTP 200，包含低基数 HTTP 请求数/耗时和服务健康 gauge。Tracing、外部通知及完整挖掘、租约故障和告警故障注入验收仍未完成。

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

### P0-1 未认证远程代码执行（审查时已实证；代码已修，待运行态复验）

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

**修复状态**：当前 `alpha_factor_pipeline.py` 的 `_is_qlib_expression` 采用有限 token/算子白名单，拒绝属性访问、下标、字符串和非白名单标识符；两条推广路径与 Qlib 执行汇点 `compute_factor_via_qlib()` 都校验表达式，计算在独立 spawn 进程池内完成。2026-10-19 对直接表达式推广入口做了部署态 HTTP 拒绝探针，非法 Python 调用表达式被拒，随后 health 正常；`/extractable` 读取到 6 条历史记录且均不可提取，未发现可用于验证恶意历史表达式的样本。有效表达式推广尚未复验。下方 exploit 细节只作为历史复现证据。

---

### P0-2 仓库内公开的 `INTERNAL_CALL_SECRET` 是一枚可用的管理员凭据（审查时已实证；代码副本已处理）

**问题**：`autoalpha-dev-internal-secret-9f3a` 硬编码在 **7 个文件**里，且**不在** `backend/shared/auth.py:25-27` 的 `_PUBLIC_INTERNAL_DEFAULTS`（该集合只有 3 个值）——于是它被当成"已配置的真实密钥"**接受**。

出现位置：`scripts/wsl/start-dev.sh:16`、`scripts/dev-web.cmd:16`、`web/vite.config.ts:43`、历史诊断 `scripts/wsl/archive/task-probe.sh:6` / `archive/smoke.sh:18` / `archive/db-smoke.sh:6`，以及当前 `scripts/wsl/quantdb-check.sh:6`。

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

**修复状态**：该公开字面量现列入 `_PUBLIC_INTERNAL_DEFAULTS`，7 处活跃配置副本已删除；Vite 不再有密钥回退，默认只监听 `127.0.0.1`。当前 WSL 私有环境文件的权限已核实为 `0600`，但仍须确保实际 `INTERNAL_CALL_SECRET` 是新随机值且旧值已轮换；不要将该值粘贴到聊天或提交中。

---

### P0-3 审查时发现的硬编码 DeepSeek API Key（历史基线；供应商侧吊销待确认）

**位置**：`scripts/wsl/start-dev.sh:31`

```bash
export DEEPSEEK_API_KEY=${DEEPSEEK_API_KEY:-<redacted>}
```

- 该值是**默认回退**，环境变量未设置时即生效；不是占位符格式。
- 它是挖掘链路的 LLM 入口，被实际读取：`backend/services/engine/alpha_agent/llm_client.py:148`、`backend/services/engine/rd_agent/llm_env.py:45`。
- 全仓库 `sk-[A-Za-z0-9]{20,}` 扫描**仅此一处**命中。
- 审查时目录尚无 Git 基线；后续已建立本地 Git 初始提交。此报告仅保留脱敏示意，不保存 Key 片段。

**修复状态**：启动脚本中的硬编码回退已移除，LLM 仅从服务端环境读取并拒绝空值/占位符。2026-10-19 对当前工作区（排除构建产物、依赖、日志和私有 runtime.env）及 Git 跟踪文件重新扫描 `sk-[A-Za-z0-9]{24,}`，均为 0 命中；不输出或记录环境变量值。供应商侧吊销状态无法从本地确认，必须在 DeepSeek 控制台吊销旧 Key 并通过受保护的 runtime env 配置新 Key。未确认吊销前，不要将仓库交给外部人员。

---

### P0-4 一个匿名请求即可让整个后端永久卡死并泄漏 30+ 进程（审查时已实证；代码已修，待运行态复验）

**位置**：`alpha_factor_pipeline.py:436` 的 `async def` 处理器内**同步阻塞**调用 `compute_factor_via_qlib`（`:459`），全程无超时。

**实证**：

1. 发送一个 `promote-by-expression` 请求后，**`GET /health` 20 秒无任何响应** → 事件循环被占死，全站不可用。
2. 该进程收到 `SIGTERM` 后**不退出**（uvicorn 的优雅退出依赖事件循环，而它正被阻塞）→ 只能 `kill -9`；systemd/容器 `stop` 会超时。
3. `ps` 实测出现 **24 个**同命令行 python 进程，全部继承 8001 监听 socket（`fd=14`），导致端口在父进程被清理后仍无法释放。一次清理共 `kill -9` 了 **34 个**进程。
4. 该请求同时会把 qlib 的 joblib worker 全部拉起（无并发上限）。

**修复状态**：当前重计算由 spawn 进程池执行，设置了并发上限和独立于 HTTP 请求生命周期的硬超时 watchdog；名额在底层进程 future 结束后才释放，因此客户端取消请求不能无限提交排队任务。超时会把旧池从全局引用摘除，再在线程中终止 worker 和关闭池，不阻塞 API 事件循环；按 executor 身份隔离重置，避免误关新池；`QLIB_KERNELS` 有上限配置。需在修复后的部署实例验证超时回收、取消/并发影响、`/health` 可用性及端口释放；历史审查的同步阻塞实现不代表当前代码。

---

## 三、P1 — 上线前必须处理

| # | 问题 | 证据 | 影响 / 修复 |
|---|---|---|---|
| P1-1 | **对外服务入口仍未就绪**：`run-wsl.sh` 使用 auth ON，但当前镜像没有登录路由，不能为用户提供可登录的服务 | `run-wsl.sh`、`app_factor_mining.py` | 当前用户已选择单人免登录；systemd 以 `AUTH_DISABLED=1` 运行。2026-10-19 实查 WSL 为 NAT、无 portproxy、Windows 8001 监听仅 `127.0.0.1`、三类 Firewall profile 的有效默认入站策略均为 `BlockInbound`，且无启用的 8001 入站 allow 规则。systemd 后端现绑定 WSL 内 `127.0.0.1:8001`；Windows localhost 可达、WSL 私网 IP 连接被拒。工作树入口启动时仅允许 `development/dev/local/test/testing` 环境启用免登录（未知环境名也 fail-closed），并校验请求 TCP 对端和 Host 均为 loopback，避免 `ENVIRONMENT=prod` 等别名漏过启动护栏或仅伪造 `Host: localhost` 绕过；该代码路径尚未运行态验证。免登录时 CORS allowlist 强制为显式 loopback 来源，`CORS_ORIGINS=*` 回退到本机开发/preview 来源，避免外站读取 localhost API；Vite 若显式配置内部密钥，其 dev/preview `/api` 代理拒绝非 loopback Host、跨站 Fetch Metadata、非同源 Origin，以及缺少 Origin 的写请求，避免浏览器跨站读写或借代理伪造内部管理员请求。免登录仍代表同机调用者共享管理员身份；未从另一台设备做网络扫描，不得对外开放。 |
| P1-2 | ~~JWT 签名密钥每次重启都重新生成~~：私有 runtime.env 中有持久化 `SECRET_KEY` | `auth.py`、`scripts/wsl/init-runtime-env.sh`；2026-10-19 只确认非空及 0600 文件权限；当前免登录模式不使用 JWT，认证开启后的签名/验证仍未运行态验证 |
| P1-3 | **逐用户 LLM Key 与用户研究内容曾写进全局可读日志** | 审查基线：`/data/alpha_agent_logs` 权限 `0755`、文件 `0644`；`grep -rl 'sk-'` 命中 **34 个文件**；577/1645 文件含用户 `direction` 文本；含 RD-Agent 的 `LITELLM_SETTINGS/**/*.pkl` | 当前根目录和一级任务目录为 `0700`；运行日志已脱敏，包含独立 `name_factor_batch.py` 进程异常堆栈。新增序列化工件扫描发现 5 个旧 `LITELLM_SETTINGS` 配置快照命中凭证字段（4 个命中常见密钥格式），已删除；剩余 1,242 个序列化文件未命中扫描覆盖的模式。任务文件不逐个 chmod 的风险由私有父目录隔离。 |
| P1-4 | ~~读取型 GET 会执行存量因子代码~~：`GET /extractable` 已移除 `exec`，改为 metadata/源码字面量提取并经过表达式白名单 | `api/routers/admin/alpha_factor_pipeline.py:_extract_qlib_expression`；静态复核完成，修复后的恶意表达式运行态 PoC 尚未重跑 |
| P1-5 | ~~任务终结写库失败可能永久显示 running~~：终态与取消写入已加入有限重试，启动/周期性 reconcile 可回收过期租约 | `launcher.py`、`task_store.py`；当前数据库与 systemd 新进程已运行，DB 故障注入/重试耗尽路径尚未验证 |
| P1-6 | ~~重启后孤儿子进程不被回收~~：新代码持久化 runner PID/PGID 与启动标识，reconcile 时只终止身份匹配的遗留进程组 | `launcher.py`、`task_store.py`；新代码已由 systemd 后端进程加载，遗留进程组恢复路径尚未在运行态验证 |
| P1-7 | **任务租约代码已加载，行为边界仍待验证**：owner fencing、租约心跳和过期回收已实现；进度更新发现持久化 owner 已更换或任务已终结时，会停止本地 runner 进程组，避免租约已失效的旧进程继续挖掘；数据准备、配置解析等耗时阶段结束后，在启动 runner 子进程前会再校验一次 owner；`ALPHA_TASK_LEASE_SECONDS` 限定在 120–3600 秒，过期回收会清空终态租约。本机 PostgreSQL 已补 `owner_id`、`lease_expires_at` 与部分索引 | `task_store.py`、`launcher.py`；2026-10-19 迁移已应用，01:55 systemd 自动重启后的 PID 87586 加载当时代码且 health 正常；queued 接管、重复启动 fencing、失租约停止及过期回收尚未做运行态验证 |
| P1-8 | **PostgreSQL 备份已部署并恢复演练通过；QuantDB 仍无副本**：E: Windows ACL 私有目录、校验和、14 天清理、夜间 timer 和失败 journal 告警已启用 | `scripts/wsl/pg-backup.sh`、`pg-restore-check.sh`、`verify-windows-backup-acl.ps1`、`install-pg-backup.sh`；2026-10-19 `pg_dump`/SHA256 成功，scratch DB 恢复 16 张表。不能防 E: 盘故障；111 GB QuantDB 尚未备份 |
| P1-9 | **本机服务已由 systemd 守护**：后端和每分钟 `/health` timer 已安装启用；受控终止主进程后自动重启，探针恢复健康，9 组路由、`failed={}` | `scripts/systemd/autoalpha-backend.service`、`autoalpha-healthcheck.{service,timer}`、`autoalpha-health-alert.service`；告警只写本机 journal，无外部通知 |
| P1-10 | ~~`passlib 1.7.4` + `bcrypt 5.0.0` 组合已损坏~~：因子挖掘安装清单固定 `bcrypt==4.0.1`，当前 systemd 后端运行于按锁文件更新后的 Python 3.12 venv | 原实测为 bcrypt 5.x 下 `CryptContext` hash 失败；`pip check` 无依赖冲突。用户认证仍关闭，未执行口令 hash/verify 路径 |
| P1-11 | ~~依赖未锁定，Python 版本约束不一致~~：项目元数据限制 Python 3.12；Linux x86_64 / Python 3.12 的 217 项锁文件逐包匹配当前 venv，所有项带 SHA256，`pip check` 无冲突 | `requirements-factor-mining.lock`、`requirements-factor-mining.txt`、`scripts/wsl/install-deps-312.sh`、`scripts/wsl/start-dev.sh`、`pyproject.toml`；其他平台需单独生成并审阅锁文件 |
| P1-12 | **前端认证开关只在显式构建环境中生效；没有登录路由** | `npm run build` 不读取 `.env.development.local`；若没设置 `VITE_DISABLE_AUTH=true`，产物不会自动进入免登录模式 | 用户已确定当前使用单人免登录，仅限本机可信环境；开发服务由 `VITE_DISABLE_AUTH=true` 与后端 `AUTH_DISABLED=1` 配套。生产部署仍未提供，不能把该选择扩大到外网；未来若制作免登录产物，必须在构建环境显式设置 `VITE_DISABLE_AUTH=true` 并保持可信网络边界。 |
| P1-13 | ~~前端健康检查依赖 Vite 专用路径，独立后端不提供构建产物~~：工作台直接请求同源 `/health`；当 `web/dist` 存在时，独立 FastAPI 入口提供静态资源与 SPA history fallback，API/WS/health 错误路径仍返回 404 | `web/src/workspace/api.ts`、`web/vite.config.ts`、`backend/services/engine/app_factor_mining.py`；`AUTOALPHA_WEB_DIST` 可指定构建目录。此形态适用于当前单机免登录部署；认证开启后的生产登录入口和网络边界仍由 P1-1 处理 |
| P1-14 | **镜像与上游 `next` 的偏离远超文档** | 审查基线曾记录 **20+ 文件 differ**。2026-10-19 按本地文件 Git blob 与 `next` 树重建清单，当前为 **80 个共同文件字节不同，其中 22 个仅换行不同、58 个内容不同；另有 88 个镜像独有文件**；比较范围内 1,797 个上游文件未纳入此精简切片。还发现并修正了 85 个 `docs/` 文件名的 Windows 解包编码损坏（内容与上游 blob 完全一致）。 | 当前清单见 `MIRROR_DIFFS.md`。58 个内容差异已逐项分类；88 个镜像独有文件按本地部署/运维用途归类。所有涉及 DB migration 或数据链路的改动仍需遵循各条目的运行验证限制；不得盲目从 `next` 覆盖。 |

---

## 四、P2 — 建议在上线前或上线后尽快处理

| # | 问题 | 证据 |
|---|---|---|
| P2-1 | ~~物料化中途崩溃会静默混入新旧列值~~：现以不完整标记 fail-closed、物化/promotion 共用锁、错误批次不注册；promotion 扫描失败整体中止以保护 `--prune`；毕业读写拒绝分区层级符号链接，恢复要求全量 `--force` | `rd_mined_materialize.py`、`promote_rd_mined.py`、`quantdb_factor_reader.py`、`factor_report/datasets.py`、`factor_research/discovery.py`；仍需崩溃注入和实际数据根上的运行验证 |
| P2-2 | ~~因子执行子进程继承整个服务环境~~：RD-Agent runner 不再持有 DB 凭据；RD-Agent 内部 `QlibFBWorkspace.execute` 的 `qrun` / `read_exp_res.py` 经 bubblewrap 禁网并过滤凭据；IC 评估、回测和物化执行器也使用隔离子进程。`/api/v1/rd-agent/factors/{id}/backtest` 的轻量回测已补入同一 bubblewrap 策略，父进程只读取有界指标 JSON，因子异常内容不写入日志/数据库；正常/异常退出均清理同进程组后代，并复用私有工作区清理器；CPU、单文件大小和进程数有子进程资源限制。固定统计算法 runner 单独保留 DB 环境。仍需完成实际 bwrap/挖掘运行态验收 | `alpha_agent/launcher.py:_strip_unneeded_child_secrets` / `_persist_rd_agent_results`、`rd_agent/qlib_conf_template.py:install_qlib_execute_patch`、`routers/rd_agent.py:_calculate_lightweight_metrics_sandboxed`、`scripts/alpha_agent/run_rd_agent.py:compute_factor_ic`、`backend/scripts/rd_mined_materialize.py:_execute_factor_code` |
| P2-3 | ~~dev 默认绕过硬件探针且运行并发 4/2~~：当前单机默认并发已降为 global 1 / per-user 1；这只限制单后端进程，多个实例不共享计数，部署仍应保持单 worker 或另加跨进程租约/信号量 | `hw_lock.py`、`start-dev.sh`、`launcher.py:323-328,1396-1404`；运行态资源压力仍需测量 |
| P2-4 | ~~回测进程注册表以 `factor_id` 为键会丢句柄~~：进程按 run_id 登记，取消按当前 run_id 查句柄；清理使用身份守卫。子进程在独立会话中启动，取消/超时按进程组终止；stdout/stderr 持续排空且每路只保留末尾 256 KiB | `routers/alpha_agent.py`、`factor_backtest/router.py`；仅静态检查，竞态及取消/超时行为未运行态验证 |
| P2-5 | ~~关键因子批量接口输入无上限~~：表达式推广、手动物化、组合优化与归档入口均在 Pydantic 层限制数组长度和 ID 长度；表达式长度与查询 limit 也有上限。两个回测入口的起止日期现在由 FastAPI 解析为 ISO 日期并拒绝反向区间，避免任意长度字符串拼入沙箱脚本参数 | `api/routers/admin/alpha_factor_pipeline.py:49-64,598`、`engine/routers/alpha_agent.py:2048,3461,2943`、`engine/routers/rd_agent.py:70-75`；静态检查完成 |
| P2-6 | ~~Vite dev/preview server 默认暴露在 `0.0.0.0`~~：两种服务均固定 loopback；校验 `VITE_HOST` 和最终解析后的 server/preview host，CLI `--host` 覆盖也会被拒绝。Preview 继承 `/api` 代理，避免免登录代理和构建产物被暴露到局域网 | `web/vite.config.ts`；配置文件独立 TypeScript 检查、前端 typecheck 与 build 通过；2026-10-19 dev 和 preview 的非 loopback CLI 覆盖均实测在监听前拒绝 |
| P2-7 | ~~journald 无显式保留边界~~：全局 drop-in 已安装并验证有效（SystemMaxUse 2 GiB、SystemKeepFree 5 GiB、RuntimeMaxUse 512 MiB、MaxRetentionSec 30 天）；任务日志仍单独保留 90 天 | `scripts/systemd/60-autoalpha-journald.conf`、`scripts/systemd/README.md`、`scripts/wsl/install-journald-limits.sh`；WSL 服务 active，journal 当前 872.2 MB，未清理历史日志 |
| P2-8 | **Prometheus metrics 和本机日志告警已接入；分布式 tracing 仍缺**：独立注册表暴露本服务按 FastAPI 路由模板聚合的 HTTP 请求数/延迟和健康 gauge，排除 `/metrics` 自身；method/status 限制标签基数。systemd 探针/磁盘/备份失败路径写本机 journal，不配置外部通知。独立入口新增 access log/request ID middleware，但当前 WSL 后端未重启加载该项 | `app_factor_mining.py`、`backend/shared/request_id.py`、`backend/shared/request_logging.py`、`scripts/systemd/autoalpha-*-alert.service`；WSL 中监控 timer active，正常服务记录已查到；低磁盘、服务故障注入仍未验证；全链路 tracing 未实现 |
| P2-9 | ~~Python `runtime_secrets.load_runtime_env()` 未被调用~~：部署入口由 Bash `load-runtime-env.sh` 加载同一私有文件，Python `get_secret()` 独立按需读取；此分工不构成启动缺陷。对 Python 启动时注入的键，文件缺失/清空/不安全时现 fail-closed，不回退旧环境快照；`set_secret()` 创建/写入前也逐级拒绝符号链接及不安全目录祖先。服务端 LLM 与数据库凭证已配置 | `start-dev.sh`、`scripts/wsl/load-runtime-env.sh`、`backend/shared/runtime_secrets.py`；2026-10-19 已以隐藏输入补齐 `DEEPSEEK_API_KEY` 与 `DATABASE_URL`；runtime.env 当前含四个必需变量且文件/父目录权限为 0600/0700，密钥未输出。旧 Key 的供应商侧吊销状态仍未知 |
| P2-10 | ~~canonical 运维脚本失败会被吞掉~~：PostgreSQL、QuantDB、依赖安装和权限变更入口均启用 fail-fast；`status.sh` 属于汇总式诊断，逐项报告 DB/health 失败并以非零退出，数据库巡检支持 `DATABASE_URL` | `scripts/wsl/*.sh`；本轮 `status.sh` 与 URL helper 通过 `bash -n`，运行态尚未验证 |
| P2-11 | ~~破坏性操作缺少人工确认/目标约束~~：venv 替换需交互确认精确路径；属主修改默认仅预览，`--apply` 时只允许 `/data` 内固定目标，拒绝顶层链接且 chown 不跟随链接，`/data` 仅开放 traverse 位 | `setup-python312.sh`、`rebuild-venv.sh`、`fix-data-owner.sh`；本轮未执行变更 |
| P2-12 | 基线配置曾包含弱默认值；当前 JWT/数据库密码已改为环境变量占位且无弱回退，监控密码字段为空。PG 初始化密码现由 Bash 生成 SQL 字面量并通过 stdin 传给 `psql`，不再进入 Python 子进程 argv；连接自检仍短暂经 `PGPASSWORD` 环境变量传递 | `config/unified/shared/security.yaml`、`config/unified/services/ai-strategy.yaml`、`config/unified/environments/{development,production}.yaml`、`config/monitoring_config.json`、`scripts/wsl/pg-setup.sh` |
| P2-13 | 磁盘风险：PG + 数据写入仍在 C: 上的 WSL `ext4.vhdx` 内；C: 低空间巡检已启用，每 6 小时检查，低于 20 GiB 时写 critical journal 告警 | `scripts/wsl/disk-space-check.sh`、`scripts/wsl/install-diskcheck.sh`、`scripts/systemd/autoalpha-diskcheck.{service,timer}`、`autoalpha-disk-alert.service`；当前 C: 约 95 GiB 可用、timer active、首次服务 Result=success。低空间触发未注入，仍需规划数据迁移/扩容 |
| P2-14 | ~~前端安装走官方 registry 且非锁安装~~：`web/.npmrc` 指向 npmmirror，启动脚本使用 `npm ci` | `web/.npmrc`、`scripts/dev-web.cmd:22` |
| P2-15 | ~~`scripts/README.md` 描述不存在的目录结构~~：已按当前脚本结构重写 | `scripts/README.md` |
| P2-16 | ~~表达式提取 GET 执行存量代码~~：当前只从 metadata/源码字面量提取并过表达式白名单；后台构建日志文件均在 `with open` 生命周期内交给 `Popen`，父侧句柄随上下文关闭 | `api/routers/admin/alpha_factor_pipeline.py:_extract_qlib_expression`、factor report/research build launchers |
| P2-17 | ~~并发推广使用 `MAX(id)+1` 分配主键，且 `MAX(order_no)+1` 没有同步~~：两个分配操作现在在同一事务级 advisory lock 内串行完成，避免主键冲突和排序号重复 | `api/routers/admin/alpha_factor_pipeline.py:register_feature_in_catalog`；`get_session()` 确认在离开上下文时才提交，锁覆盖分配与插入；Python 语法检查通过，数据库并发写入尚未运行态验证 |
| P2-18 | ~~推广接口收到 `auto_train=true` 会静默忽略，且 `/promote` 曾无条件返回 `auto_train_triggered=true`，但训练调用仍是 TODO~~：两个推广入口现在都保持 false，并在推广成功时通过 `errors` 明确提示用户手动启动训练 | `api/routers/admin/alpha_factor_pipeline.py:/promote`、`/promote-by-expression`；Python 语法检查完成，响应兼容性尚未做运行态验证 |

---

## 五、P3 — 代码/仓库卫生

- 前端旧参考模块仍保留；当前 `src/` 有 81 个 TS/TSX 文件，其中 `tsc --listFilesOnly` 从 `src/main.tsx` 追踪到 20 个，61 个文件不在当前应用导入图中。`tsconfig.json` 已改为只检查 live entry graph，Vite build 仍解析和打包实际路由依赖。
- ~~5 个疑似死依赖~~：`react-redux`、`@reduxjs/toolkit`、`clsx`、`tailwind-merge`、`remark-gfm` 已从 slim app 根 manifest 与 lockfile 移除；它们只被未挂载旧模块引用（`remark-gfm` 无源码 import）。恢复旧模块时需显式恢复依赖。
- `tsc` 的 live entry 检查与 package 裁剪已落地；原工作区执行 `npm ci` 时因当前 Vite 进程占用 `esbuild.exe` 返回 EPERM。之后 `npm install --no-audit` 按 lockfile 恢复依赖，`npm ls --depth=0` 无缺失/无效项，前端 `:3000` 返回 200；未停止正在使用的 dev server，也未宣称这次是干净环境的 `npm ci` 演练。
- ~~7 条 npm advisory 全在未使用的 Tailwind 构建链~~：live CSS 仅使用 Autoprefixer；已从 package 与 PostCSS 插件中移除 Tailwind 3 和 `tailwindcss-animate`，删除未用的 `tailwind.config.cjs`，旧 CSS 参考文件保留。2026-10-19 官方 npm audit 报告 0 vulnerabilities；`npm.cmd run typecheck` 通过；`npm.cmd run build` 成功（3,797 模块），仍有两个约 600 KB chunk 的体积警告。
- ~~`authService.ts` 曾打印整个请求/响应体及 Axios 错误对象（登录路径可能带明文口令/token）~~：已移除正文日志；`authService`、`authSlice` 不再序列化 Axios 错误对象，通用错误日志只保留类型、错误码、时间戳。认证页面仍未挂载；`npm.cmd run typecheck` 通过。
- 两个大 chunk 仍待按需优化（当前 `Chart-*.js` 612.65 KB / 208.89 KB gzip，`index-*.js` 619.43 KB / 207.40 KB gzip）；build 的 500 KB 未压缩阈值会告警。暂未加 `manualChunks`，避免只拆文件却不减少首屏传输。
- ~~`ui.tsx` 卡片点击语义、图表 `aria-label` 角色缺失~~：可操作卡片补 `role="button"`、焦点态和 Enter/Space 处理；图表容器补 `role="img"`。类型检查通过，键盘/读屏实际交互尚未做浏览器验收。
- ~~`App.tsx` `path="*"` 静默重定向，无 404 页~~：现在展示工作空间内的 404 状态，并提供返回首页操作；保留旧版已知路由的兼容重定向。
- ~~`ui.tsx` 递归无界 JSON stringify~~：详情序列化现在限制深度、节点、属性/数组项、单字符串和总输出长度，并标记循环引用；类型检查通过。
- ~~中文/标点因子名经 `_sanitize_feature_key` 清洗后会相撞并覆盖~~：普通短 ASCII 名称保留既有 key；含非 ASCII、空白/标点、压缩下划线或会被截断的名称追加稳定 SHA-256 短后缀，保证不同输入生成不同的合法 key。
- `index.html` 本身不承载 HTTP 安全策略；Vite dev 已加基础安全响应头，Vite preview 对构建产物加严格 CSP（允许 Ant Design 所需 inline style，脚本限同源）。开发态 CSP 暂不启用，因为 React Refresh 注入 inline bootstrap；未来静态生产宿主仍需配置自己的响应头。
- `lucide-react ^0.390.0` 被 caret 冻结在 `0.x`（永远升不上去）。
- ~~`index.html` / `main.tsx` 引用未随应用提供的 Inter / Noto Sans SC~~：已统一使用系统字体栈，不再产生隐式字体下载或因缺字库而回退；具体字形随操作系统字体而定。
- ~~`vite.config.ts` 中 `VITE_API_BASE_URL` 的 `define` 无调用点~~：已移除；`process.env.NODE_ENV` 仍保留，因为认证错误提示和 Redux devtools 有实际引用。
- ~~混合解释器的 `.pyc` 被跟踪进 Git~~：当前 `git ls-files` 没有跟踪任何 `.pyc`/`__pycache__`；根 `.gitignore` 排除 `__pycache__/` 与 `*.py[cod]`。
- `docker/` 只剩 24 MB 字体，无 Dockerfile/compose；`config/postgres/*.conf`、`config/redis/*.conf` 是未使用的上游副本，DR 时误导。
- ~~本目录不是 git 仓库~~：本地 Git 基线存在（当前 HEAD `c5134d4`），根 `.gitignore` 排除本地密钥、缓存和构建产物；仓库无 remote，尚不能提供远端备份/PR 工作流。
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
- PG 只绑 `127.0.0.1`；WSL2 为 NAT 模式、8001 relay 只绑 loopback、Windows 防火墙 `BlockInbound` → **当前 8001 不可从局域网直连**；Vite 开发服务器也在配置层拒绝非 loopback 绑定。
- 数据分布合理：111 GB QuantDB 在 E:（**独立物理盘**），与 C: 上的 vhdx 分离，单盘故障不会同时带走两者。
- 挖掘任务事后可诊断：`/data/alpha_agent_logs/<hash>/Loop_N/` 保留了会话轨迹。

---

## 七、未能验证（诚实标注）

1. 未做完整挖掘端到端跑（会改状态、耗时长）。
2. 未验证 `pg_hba.conf` 与 PG 角色实际权限（无免密 sudo，文件不可读）。
3. 未执行 `pip install` 复现依赖（会改状态）；版本漂移是**基于现有 venv 实测**，非全新安装结果。
4. **未验证该 DeepSeek Key 是否仍有效**（未对外发请求）——但无论有效与否都必须轮换。
5. P0-1 的 **HTTP 层干净复现**：HTTP 路径已取得"eval 确实执行"的痕迹（0 字节文件 + 每 worker 一次的 shell 报错），干净的内容型证明是在**直接调用同一函数**时取得的。未在 HTTP 层同时拿到 200 + 内容，因为该请求本身会把服务卡死（即 P0-4）。
6. 未在运行态覆盖回测取消/立即重跑/延迟收尾竞态（P2-4 已改为 run_id 句柄，但仅有静态验证）。
7. 未重启 WSL/PG 实测"PG 自启、应用不自启"（按 `systemctl is-enabled` 与缺少 unit 推断）。
8. 未实测 `E:\quantdb` 字节数（9p 上 `du` 2 分钟未完成）。

---

## 八、上线前检查清单

**必须先做（P0，任一未完成都不得对外暴露）**

- [ ] 到 DeepSeek 控制台**吊销并轮换**旧 Key；仓库回退值已删除，启动脚本缺值即拒绝；供应商侧状态未知，需账号持有人处理。
- [x] 补齐现有 `~/.config/autoalpha/runtime.env`：已隐藏输入服务端 LLM Key，并保存当前进程的 `DATABASE_URL`；文件/父目录权限为 0600/0700。供应商是否已吊销历史泄漏 Key 仍需人工确认。
- [x] 旧 `INTERNAL_CALL_SECRET` 已加入 `_PUBLIC_INTERNAL_DEFAULTS`；运行脚本和 Vite 配置不再包含可用副本。实际部署仍需自行生成随机值。
- [x] `web/vite.config.ts` 默认绑定 `127.0.0.1`，移除了密钥兜底；未提供密钥时不代注入身份头。
- [x] `config/` 中 JWT/数据库密码不再使用历史弱默认值；敏感配置改为环境变量占位或空值，PostgreSQL 初始化脚本不再把密码放进 Python 子进程 argv。
- [x] 请求表达式经过语法白名单并在受限子进程中执行；GET 提取分支不再执行存量代码。
- [x] `promote*` 重计算移出事件循环，设置超时、并发上限，并限制 qlib worker。

**上线前应做（P1）**

- [x] 认证形态已确定为仅本机可信开发环境免登录；不支持对外或生产暴露。若要对外服务仍需登录/可信认证网关。
- [x] 当前免登录形态已确认仅供本机可信开发环境；`run-wsl.sh` 仍不得对外开放，未来生产入口需独立可信认证网关或其他认证边界。
- [x] 私有 runtime.env 已有非空 `SECRET_KEY`（文件 0600）；当前免登录部署不使用 JWT。若启用认证或生产部署，仍需按届时密钥策略做运行态验证。
- [x] LLM、Embedding、MinerU 凭证仅从服务端环境解析；旧 MinerU Redis 读取与原始 Profile 凭证网关已移除/禁用，历史用户密钥来源的解析任务明确失败。
- [x] 任务日志目录/新文件权限已收紧；ASGI 日志处理器、HTTP 异常、任务/文档历史、直接错误响应与任务日志 API 已加环境密钥脱敏。
- [x] 当前 WSL 日志根及所有一级任务目录权限已实际修正为 0700。
- [x] `subprocess_stdout.log` 通过有界流式拷贝写入，并脱敏父/子进程环境中的凭证。
- [x] runner 退出后对 RD-Agent `common_logs.log` 做流式密钥脱敏并原子替换。
- [x] 扫描 RD-Agent 序列化工件中的凭证字段和常见密钥格式；删除 5 个命中的旧 `LITELLM_SETTINGS` 配置快照，并在 runner 退出或启动时处理遗留 runner 后清除新生成的同类快照。扫描未反序列化文件，不保证发现未知格式的密钥。
- [x] 安装 `scripts/systemd/60-autoalpha-journald.conf` 并核验合并配置/生效；日志上限是全局 journald 策略，任务文件仍按 90 天 GC。
- [x] `GET /extractable` 已去掉 `exec` 分支，并限制返回数量。
- [x] 挖掘任务表增加进程 owner 与 180 秒租约；当前进程心跳续租，启动及每 30 秒回收过期活动任务；终态写入限定 owner 和活动状态。
- [x] 执行任务租约数据库迁移：本机表已具备 `owner_id`、`lease_expires_at` 和部分索引。
- [ ] 在加载新代码的进程中验证 queued 接管、重复启动 fencing 和过期回收路径。
- [x] 活跃任务状态持久化 runner PID/PGID、启动标识；重启时只终止身份仍匹配的遗留进程组。
- [x] PostgreSQL 备份目录改用受保护的 Windows DACL（DrvFS POSIX mode 不可信）；systemd nightly timer 已启用，实际备份、SHA256 和 scratch DB 恢复演练通过（16 张表）。
- [ ] 为 111 GB QuantDB 建立独立物理盘或异机副本；当前 E: 同时保存 QuantDB 和 PostgreSQL 备份，E: 故障仍是共同失效点。用户决定本轮暂缓数据复制，继续保留现有软链接。
- [x] 新增 systemd 后端 `Restart=always` unit、`/health` timer 和 journal 告警模板。
- [x] 安装并启用每分钟 `/health` 探针及 journal 告警；已验证手动后端和 systemd 后端探测 Result=success。
- [x] 已启用 `Restart=always` 后端 unit；受控终止主进程后确认 PID 更换并恢复 `/health`。失败告警写本机 journal，不接外部通知。
- [x] `bcrypt==4.0.1` 已锁定，兼容 passlib 1.7.4。
- [x] `requirements-factor-mining.lock` 已生成（Linux x86_64 / Python 3.12，SHA256 hashes）；安装入口使用 `--require-hashes`，启动脚本硬校验 Python 3.12。
- [x] 独立 FastAPI 入口可同源提供 `web/dist` 和 SPA history fallback，工作台统一请求 `/health`；该形态供当前本机免登录部署使用，生产认证与外网入口仍由 P1-1 单独处理
- [x] 重建"本镜像 vs 上游 `next`"的 blob 差异清单，修复 85 个文档文件名编码错乱，并更新 AGENTS.md（P1-14；可用 `scripts/mirror-diff-inventory.py` 重建清单）
- [x] 按用途分类 82 个镜像独有文件，区分运行时资产、部署/验证脚本与一次性诊断工具；逐条路径见 `MIRROR_DIFFS.md`（不得据此盲目覆盖或删除）

**上线后尽快（P2/P3）**

- [x] 挖掘任务终态日志按 90 天留存清理，启动与每日 GC；目录权限保持私有。
- [x] 安装并运行态验证 journald 全局上限（系统 journal 2 GiB、runtime journal 512 MiB、30 天；安装时占用 872.2 MB）；任务日志另有 90 天终态 GC。
- [x] Prometheus `/metrics`（基础请求数、延迟、服务健康）；[ ] 分布式 tracing；[x] 本机 journal 告警（健康/磁盘/PG 备份 timers active，正常记录已查；低空间与服务故障触发仍待验证）。用户选择本机日志，不需要外部应用通知。
- [x] `rd_mined` 物料化/promotion 期间先标 not-ready；读路径阻断、失败时不注册，完成后才恢复可读（静态检查完成，崩溃恢复需运行态验证）
- [x] 单机挖掘并发默认限制为 1（全局及每用户）；提高上限需部署者根据实测显式设置，且该容量计数不跨后端进程共享。
- [x] 因子回测进程句柄按 `run_id` 登记并按句柄身份清理，避免取消后重跑时旧任务拆掉新句柄（竞态需运行态验证）。
- [x] 因子批量推广、手动物化、组合优化与归档请求在 Pydantic 层限制列表大小和 ID 长度；表达式与分页 limit 有独立上限。
- [x] 挖掘 runner 与因子回测子进程按需裁剪密钥环境变量；凭证名归一化额外覆盖 `TENCENT_SECRET_ID`、`AWS_ACCESS_KEY_ID` 与 SDK 的 `SecretId` 别名。
- [x] 清理 live frontend 的死依赖与 Tailwind 构建链；官方 npm audit 为 0 vulnerabilities。未挂载页面和 CSS 按 `AGENTS.md` 保留为上游参考副本，不纳入运行依赖
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

*审查基线来自只读审查 + 受控实证。之后的代码修复、运行态变更和验证按时间记入上文；“审查时”的环境记录与证据保留为历史。当前已验证后端健康/自动重启、Prometheus `/metrics`、PostgreSQL 备份校验/恢复及 Vite 到后端的基础代理请求；完整 UI/挖掘端到端、租约故障路径与远程网络扫描仍未完成。*


## 运行态更新（2026-10-19）

- 用户确认 API 凭证存服务端环境变量、当前不启用用户登录；本机 `AUTH_DISABLED=1` 按该范围运行。
- 私有 `/home/qusong/.config/autoalpha/runtime.env` 已补齐非空 `DEEPSEEK_API_KEY` 与 `DATABASE_URL`，同时保留 `INTERNAL_CALL_SECRET`、`SECRET_KEY`；目录 `0700`、文件 `0600`，检查过程不输出值。仅确认非空与启动成功，未发起真实 LLM 计费请求，也无法从本地确认旧泄漏 Key 已在供应商侧吊销。
- 已安装并启用 `autoalpha-backend.service`。活动任务数为零时切换手动 Uvicorn，随后受控终止服务主进程验证 `Restart=always`：PID 从 65607 变为 66080，`NRestarts=1`；`GET /health` 为 healthy，`auth_disabled=true`、9 组路由、`failed={}`。
- `owner_id`、`lease_expires_at` 和租约索引已在数据库；新进程已加载租约代码，但 queued 接管、重复启动 fencing、过期回收仍未做故障注入。免登录仅限当前本机/受控开发部署，不构成对外发布授权。
- 网络边界复核：WSL networking mode=`nat`，WSL IP=`172.21.23.211`；`netsh interface portproxy show all` 无映射，Windows 仅监听 `127.0.0.1:8001`，三类 Firewall profile 均有效 `BlockInbound,AllowOutbound`，未找到启用的 8001 入站 allow 规则。Windows 主机 `Test-NetConnection` 对 localhost 与 WSL IP 均成功；未从局域网第二台设备扫描。

- 2026-10-19 备份闭环：`/mnt/e/AutoAlphaBackups` 的 Windows ACL 已验证仅允许当前账户、SYSTEM、Administrators；`autoalpha-pg-backup.service` 成功，`autoalpha-pg-backup.timer` active/enabled（每日 02:30 左右）；checksum 校验通过。`autoalpha_restore_20261019` scratch DB 从 dump 成功恢复 16 张 public 表并保留供检查。曾有一次 systemd 首跑失败（服务环境 PATH 不含 `powershell.exe`），告警已写入 journal；改为 WSL 绝对路径后成功。
- 2026-10-19 前端凭证面收口：遗留用户中心 LLM Key 读/写/连通性测试方法，以及 MinerU 用户密钥保存 payload 已从客户端服务层删除；API 状态仅保留服务端环境配置状态。旧 MinerU 用户级记录的 DELETE 清理能力仍保留。类型检查通过；当前六路由应用未挂载用户中心旧页面。
- 2026-10-19 配置提示统一：MinerU 后端缺配置响应和运行时错误消息不再指引用户级设置 UI，改为提示在受保护的部署环境配置对应变量并重启；清除无调用点且仍生成用户级 Embedding Key payload 的 `embeddingPayload.ts`。`npm.cmd run typecheck`、改动 Python 文件 `py_compile` 均通过；全仓搜索后仅剩只读 `getLlmConfig()` 状态查询。PID 86517 于 01:52 启动，已加载当时后端代码。
- 2026-10-19 约 01:35 CST 的 `sudo` 重启命令因交互认证未执行，之后在最近 50 条任务无 pending/queued/running 的前提下，向服务主进程发送 SIGTERM；systemd 按 `Restart=always` 自动启动 PID 85230（01:48:53 CST）。为隔离指标暴露范围，再次确认 0 活动任务后终止 PID 85230，systemd 于 01:52:19 CST 启动 PID 86517。重启后 `/health` healthy、免登录开启、9 组路由、`failed={}`；`/metrics` HTTP 200，注册表仅含 `autoalpha_*` 指标，并验证 `/health` 路由模板标签及排除 `/metrics` 自身。sudo 提示会话已取消，未输入或保存密码。
- 2026-10-19 01:55 CST 指标标签基数收敛：限制 method 为常见 HTTP 方法或 `OTHER`，status 归为 `1xx`–`5xx` 或 `other`；Python 语法检查和 `git diff --check` 通过。重启前任务历史 13 条中活动任务为 0，systemd 自动重启至 PID 87586；健康检查恢复后 `/health` healthy、免登录开启、9 组路由、`failed={}`，`/metrics` HTTP 200 且仅含 `autoalpha_*` 指标，样本标签为 `GET`、FastAPI 路由模板、`2xx`。启动瞬间一次请求被拒绝，数秒后健康并稳定。认证开启的 metrics 路径未运行态验证。
