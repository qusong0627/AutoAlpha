# 统计派挖掘 · Tier 3 体系化（滚动再选择 / 子窗稳定性 / 相关性去重 / IC 加权合成）—— 规划与任务单（2026-10-10）

> 承接 `因子挖掘_机构级挖掘与验证框架_规划.md`（T-MV）与统计派 P2 交付。
> 统计派分层：Tier 1（多周期 IC/FDR/样本外/可交易性，已交付）、
> Tier 2（28 候选/中性化/分组单调性，已交付）、**Tier 3（本单）**。

## 0. 目标（用户诉求逐条驱动）

| 用户诉求（逐字） | 本单的答案 |
|---|---|
| 「滚动再选择」 | T-S3-01：展开窗检查点上重放 Top-K 排名 → ``reselect_rate``（**2026-10-11 修订：降为审计量**，闸改折间符号一致性，见 §4） |
| 「IC 半衰期/子窗稳定性」 | T-S3-02：多周期 |RankIC| 对数线性衰减拟合 + 逐折 IC/符号一致性（**修订：折间符号一致性升为唯一稳定性闸**） |
| 「因子相关性去重（与 P3 去重指纹合流）」 | T-S3-03：入选集内逐日截面秩相关贪心去重（0.9 拒/0.8 警，与物化器同口径）+ 稳定因子身份 = P3 任务指纹（**修订：选择期带递补**，见 §4） |
| 「IC 加权合成因子」 | T-S3-04：入选因子按目标周期 RankIC 加权合成，自带完整 IC/OOS/分组证据落库 |

## 1. 设计口径（先说死，再实现）

### 1.1 滚动再选择（T-S3-01）

- **检查点**：把挖掘窗按**全体候选 IC 日的并集日历**切成 ``stability_folds``（默认 4）个等份；
  检查点 = 第 2..K 折末（展开窗，至少两折才能比较）。
- **重放规则**：每个检查点上，用「折 1..j 的合并日均 RankIC」按 |RankIC| 重排 Top-K，
  记该候选是否入选；``reselect_rate = 命中检查点数 / (K-1)``。
- **诚实边界**：重放只重演**排名阶段**——不重跑 BH-FDR / 样本外（这两个闸需要各自的
  周期估计，逐检查点重算是另一个量级的机制，且语义会漂）。文档与代码注释都写明：
  候选池 = 检查点上「有可算 IC 日」的全部候选（比生产选择池宽），因此率值是**保守**的。
- **闸**：``eligible``（每折有效日 ≥ ``min_fold_days``）且 ``reselect_rate <
  reselect_min_rate``（默认 0.5）→ 拒（``滚动再选择不稳定``）。**不可判定不拒**：
  折太短就如实标 note，不拿假精确拒人。
  **2026-10-11 修订（P0）**：此闸在真实数据上有刀口误杀缺陷（csi300 挖出 19/28 候选
  命中 0/3 却被拒），已被**折间符号一致性闸**取代——``reselect_rate`` 保留计算但降为
  审计量，不再参与判决；**≥0.75 折与全窗同号** 才放行。推导与实证见 §4。
- **口径**：每候选用自己的 best horizon 的 RankIC 日序列（与生产 Top-K 排序同一条线）。

### 1.2 IC 半衰期与子窗稳定性（T-S3-02）

- **半衰期**：对多周期 ``|RankIC(h)|``（与 best horizon 同号的点，≥3 个）做
  ``log|IC| = a − λh`` 最小二乘；``half_life = ln2/λ``（λ ≤ 0 → NaN，如实标「不衰减」）。
  纯审计量，不进闸（半衰期是描述，不是判据）。
- **子窗稳定性**：逐折 RankIC 均值（``fold_ics``）+ 与全窗同号折占比
  （``fold_sign_consistency``）——进报告；**闸**用 1.1 的 ``reselect_rate``
  （直接回答「当初会不会被选中」，比符号占比更贴选择语义，避免两个近义闸叠加误杀）。
  **2026-10-11 修订（P0）**：两者对调——**闸 = ``fold_sign_consistency ≥
  min_fold_sign_consistency``（默认 0.75）**，``reselect_rate`` 降为审计量。
  折占比是量级无关的形态量，不受月末最强调仓/折窗切法影响；率值依赖检查点位置，
  真数据上大量「折折稳定」的候选因检查点错位得 0。见 §4。

### 1.3 相关性去重（T-S3-03）

- **值级口径**：入选集内两两「逐日截面 Spearman（先秩再 Pearson）的时间均值」，
  窗口 = 挖掘窗 ∩ 可交易 ∩ 中性化后（与选择证据同一批值），单日有效样本 <
  ``min_names_per_day`` 的日子不计。
- **贪心**：按 |RankIC| 降序，逐个与已保留者比；最大 |ρ| ≥ ``dedup_corr_reject``
  （0.9，与物化器同阈值）→ 拒（拒因带对照 key 与 ρ）；``[warn, reject)`` → 保留但记
  ``corr_warn``。**不补位**：被去重挤掉就是挤掉（Top-K 是闸，不凑数——与既有纪律一致）。
  **2026-10-11 修订（P0）**：选择期改为**带递补**（``greedy_select``）——被拒者不占
  K 席，后续候选继续递补直至席位填满或候选走完；Top-K 是**席位**（信息量相同的因子
  只留代表），不是「先到先得、挤掉就空着」。``dedup_selected`` 保留为 k=∞ 事后全判
  特例（物化器/审计契约不变）。排序键同步从 |RankIC| 改为**选择分**
  （``selection_score = |RankIC| × fold_sign_consistency``，无稳定性证据时权重取 1）。
- **与 P3 指纹合流**：因子 id 从 ``md5(task_id:candidate_key)`` 改为**稳定身份**：
  ``md5(statistical:user:market:universe:candidate_key:task_fingerprint)``。
  同一份挖掘配置（同 P3 指纹）重跑 → 同一行**幂等刷新**，不再堆重复行；
  不同配置 = 不同实验，各留一行（跨版本的值级查重仍归物化器，边界写死在注释里）。
  指纹由 launcher 注入 ``QM_MINING_TASK_FINGERPRINT``；手工直跑无指纹时如实退回
  任务级身份（``identity_scheme=task``，metadata 记录，可追溯）。

### 1.4 IC 加权合成因子（T-S3-04）

- **分量/权重**：去重后的入选因子；目标周期 h* = 可用分量最多的周期（并列取小）；
  ``w_i = RankIC_i(h*) / Σ|RankIC_j(h*)|``（符号保留，Σ|w| = 1），权重**取 6 位小数
  后双侧同值**（表达式与 pandas 用同一份舍入权重，不许一边全精度一边截断）。
- **合成口径（可复现优先）**：``composite = Σ ŵ_i · z_i``，``z_i`` = 分量表达式的
  **逐 symbol 滚动 z 分数**（窗口默认 60，min_periods=1 的 Mean/Std，与 qlib 算子
  语义一致）——qlib 0.9.7 **没有截面算子**（``Rank`` 是滚动百分位），滚动 z 是
  唯一双侧同式的量纲统一手段；因此合成因子**不做截面中性化**（中性化是离线回归，
  表达式复现不了）——口径写进 metadata，落库 ``factor_code`` = 单行 qlib 表达式，
  与分量同链路回测，等价性由测试对拍 qlib 求值钉死。
- **评价**：与候选同一套机器（多周期 IC → best → 样本外闸 → 分组单调性），
  **不进 BH-FDR**（它不属于原始候选池，分量已各自过检；metadata 注明）。
  样本内可评估 + 样本外闸（若配）通过 → 落库为独立因子行（``is_composite: true``，
  identity 同 T-S3-03，candidate_key = ``ic_weighted_composite``）。
- **诚实降级**：分量 < 2 / 权重退化 → 不合成（emit 如实出声），不硬凑。

## 2. 任务单

| 号 | 任务 | 产物 |
|---|---|---|
| T-S3-01 | 滚动再选择 | `statistical/stability.py`（`reselect_statistics`）+ 引擎闸 + 测试 |
| T-S3-02 | IC 半衰期/子窗稳定性 | 同模块（`ic_half_life` / 逐折统计）+ 报告字段 + 测试 |
| T-S3-03 | 相关性去重 + 稳定身份 | `statistical/dedup.py` + `ic_stats.daily_rank_corr` + runner 身份改造 + launcher 指纹 env + 测试 |
| T-S3-04 | IC 加权合成因子 | `statistical/composite.py` + 引擎评价/落库 + qlib 等价对拍 + 测试 |

## 3. 施工记录（2026-10-11 回填；全部为实测证据）

**结论：四条机制全部落地。** 测试面 313 passed / 0 failed / 0 skipped（17 个模块：
统计派 12 + 挖掘接线 5），ruff 全绿；合成因子的 qlib 表达式在容器内对真实 qlib 数据
**逐点对拍通过（未 skip）**。

### 3.1 交付物

| 机制 | 实现 | 契约测试 |
|---|---|---|
| 滚动再选择 + IC 半衰期 | `statistical/stability.py`（188 行） | `test_statistical_stability.py`（214 行） |
| 相关性去重 + 稳定身份 | `statistical/dedup.py`（156 行） | `test_statistical_dedup.py`（269 行） |
| IC 加权合成 | `statistical/composite.py`（157 行） | `test_statistical_composite.py`（272 行，含 qlib 逐点对拍） |
| 引擎闸/报告字段/落库 | `statistical/engine.py` + `scripts/alpha_agent/run_statistical_mining.py` + `statistical.yaml` v1.1.0（7 阶段） | `test_statistical_engine_tier3.py`（314 行）+ runner 增补 5 例 + freq env 接线增补 2 例 |

### 3.2 行为闭环实测（合成面板 seed=7，40 只 × 120 日；挖掘窗第 8..79 日、4 折）

| 观测 | 实测值 |
|---|---|
| early（全程稳）RankIC / 命中率 | 0.1368 / 0.6667（2/3 检查点） |
| late（仅末折爆发）整窗 RankIC | 0.2319 —— **整窗最强** |
| late 命中率 / 判决 | 0.3333（1/3）→ 拒：`滚动再选择不稳定（1/3 检查点入选）` |
| late 逐折 IC | (-0.0316, 0.0307, -0.0068, 0.9353) —— 末折独强，证据与判决一致 |
| 闸开/闸关翻转 | 闸开 `selected=('early',)`；闸关 `selected=('late',)` —— 闸不是装饰 |
| 去重 | clone 与 early 秩同序 ρ=1.0 → clone 被拒，`kept=('early',)` |
| 合成 | 权重 (0.5, 0.5)，h\*=1，min_history=5 = 2 + (z 窗 4 − 1) |

### 3.3 施工中发现的问题与修正（防复发）

1. **夹具配平靠探针不靠猜**：初版 β=0.6 / γ=0.15 / 激活日 60 时，early 通道 RankIC 饱和
   在 ≈0.55、late 上限 ≈0.28 ——「关闸后 late 当选」物理上不可能，红的先修的是夹具。
   重配 β=0.1 / γ=0.12 / 激活日 63（信号严格落第 4 折）后 0.1368 vs 0.2319 闭环成立。
2. **末检查点的累计窗 = 整窗**（K=4 时第 3 检查点即全窗）：窗尾爆发**必然**命中末检查点，
   所以「任何检查点命中即算稳定」是错判据，必须用率阈值。实测 steady 的 rate 也是 2/3
   （末点被 late 反超）—— 这正是闸门存在的理由（文档串已写进 stability 测试）。
3. **`CandidateReport` 无 `eligible` 字段**：「可判定」是引擎内部状态；测试钉折结构
   （fold_ics / reselect_checkpoints ≥ 2）与拒因，不为测试伪造报告级字段。
4. **Tier 3 闸默认 ON 波及旧测试**：`top_k overflow` 与 `no-validation-window` 两例显式
   `require_stability=False`（原因被稳定性拒因顶掉的假红），各自意图在文档串写明。
5. **manifest 7 阶段波及 3 个接线测试**：阶段表/标签/百分比（evaluate=3/7→42）、
   封顶用例改用末阶段 composite、unknown-stage 期望 7+1。

### 3.4 写进 metadata 的「刻意不做」（回测方不会误当同口径）

- 合成因子：`fdr_applied=False`（不属于原始候选池，分量已各自过检）、`neutralized=False`
  （中性化是离线回归，单行 qlib 表达式复现不了）；
- 去重：**不补位**（Top-K 是闸，被挤掉即掉）；
- 重放只重演**排名阶段**（不重跑 FDR/OOS——语义会漂）；率值口径偏保守（重放池比生产
  选择池宽，注释写明）。

### 3.5 测试纪律（本单遵守的边界）

- 落库链路用**替身记账**（`_RecordingPersistence`）+ 身份 md5 契约钉住 —— 测试不写/不删
  生产 `rd_agent_factors` 行（既往事故纪律，见 `test_statistical_runner.py` 模块 docstring）；
- 幂等靠 `ON CONFLICT (factor_id) DO UPDATE`，身份 = `md5(statistical:user:market:universe:
  candidate_key:task_fingerprint)`；缺指纹如实退回 `identity_scheme=task` 并在 metadata 记录；
- qlib 等价对拍在容器内真跑（`QM_TEST_QLIB_DIR` 默认 `/data/qlib/cn_data` 存在）；
- 本单全过程未重启容器、未触碰生产数据行。

## 4. 修订（2026-10-11 · P0 升级：重选闸刀口缺陷 + 选择期递补）

### 4.1 缺陷实证（真实数据，非构造）

csi300 真跑一批 28 候选：**19 个候选 3 个检查点命中 0，但四个折的 IC 与全窗全部同号**
（折折稳定，只是各折强弱排序在检查点上会洗牌）——``reselect_rate`` 判据测的是
「检查点时刻的相对排名是否会变」，而 Top-K 出线靠的是**全窗绝对信号**；两者在
候选间 IC 量级接近时天然不一致（刀口）。旧闸把这类稳定候选整批误杀（当日实跑
入选仅 4 个的直接机制之一）。

### 4.2 新口径（本节为准，正文 §1 已就地修订）

| 项 | 旧 | 新 |
|---|---|---|
| 稳定性闸 | ``reselect_rate ≥ 0.5`` | ``fold_sign_consistency ≥ 0.75``（``min_fold_sign_consistency``，Config 可调；``reselect_rate`` 降为审计量） |
| 排序键 | ``\|RankIC\|`` | ``selection_score = \|RankIC\| × fold_sign_consistency``（无稳定性证据 → 权重取 1；无 best horizon → NaN，不入选） |
| 选择期去重 | 先到先得、不补位 | ``greedy_select`` 席位制：被拒者不占席、后续候选**递补**；``provider`` 按需取值（K 席之外不白算）；``dedup_selected`` 保留为 k=∞ 事后全判 |
| 默认双窗 | 挖掘/检验共享边界日 | 检验窗从**挖掘窗结束次日**起（边界日收益不喂两侧 label） |

出处：FactorEngine（arXiv 2603.16365）的 FS-fitness = 幅度×稳定度 + 选择期贪婪去重；
本仓落地为折间符号一致性 × |RankIC| 与席位递补，未引入其增量回测框架（见后续 P1）。

### 4.3 验收证据

- Tier-3 夹具重校准（β=0.05 / 早期 γ=−0.006 / 末折 γ=0.3）：early（折折稳）cons≈1.0
  入选、late（末折爆发）cons≈0.25 被拒；**旧闸下两者判决相反**（late rate 3/3 vs
  early 0/3）——夹具精确复现实盘病态，闸开/关判决翻转有测试钉住；
- 真实注册表探针：``gap_drift_5d`` 与 ``reversal_5d`` ρ=−1.00 → 被拒，
  ``close_position_20d`` **递补进第 3 席**；``skew_20d`` cons=0.25 / ``vol_20d``
  cons=0.5 被拒、``vol_60d`` cons=1.0 放行——与实盘刀口证据同源；
- ``selection_score`` 随 metadata 落库（审计 Top-K 排序与去重步行序）。
