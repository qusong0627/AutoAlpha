import { Fragment } from 'react';
import { useNavigate } from 'react-router-dom';
import { Table } from 'antd';
import { BarChart3, Bot, BookOpen, Database, FlaskConical, LifeBuoy, SlidersHorizontal, Sparkles } from 'lucide-react';
import { Page, Panel, SpecPanel, Status } from './ui';
import type { Row } from './api';

/** 上手五步：一行流程图（步骤 ─箭头→ 步骤），每步可点进对应页面。 */
const FLOW = [
  { tag: '第 1 步', color: '#2563eb', icon: <SlidersHorizontal size={17} />, title: '参数配置', desc: '默认股票池 · 算法 · 数据窗口', to: '/mine' },
  { tag: '第 2 步', color: '#7c3aed', icon: <Sparkles size={17} />, title: '发起挖掘', desc: '描述方向，AI 逐轮演化', to: '/' },
  { tag: '第 3 步', color: '#0891b2', icon: <Bot size={17} />, title: '任务中心', desc: '进度 · 日志 · 决策轨迹', to: '/tasks' },
  { tag: '第 4 步', color: '#059669', icon: <Database size={17} />, title: '因子库', desc: 'IC / ICIR · 生命周期', to: '/factors' },
  { tag: '第 5 步', color: '#9333ea', icon: <BarChart3 size={17} />, title: '回测中心', desc: '样本外回测 · 导出 PDF', to: '/backtest' },
];

/** 指标口径小表（label / 含义 / 口径），左对齐以免和页面居中版式打架。 */
function Glossary({ rows }: { rows: [string, string, string][] }) {
  return <Table<Row> rowKey="metric" pagination={false} size="small" dataSource={rows.map(([metric, meaning, how]) => ({ metric, meaning, how }))} columns={[{ title: '名称', dataIndex: 'metric', width: 150 }, { title: '含义', dataIndex: 'meaning' }, { title: '口径', dataIndex: 'how', width: 320 }]} />;
}

export default function HelpPage() {
  const navigate = useNavigate();
  return (
    <Page
      eyebrow={<><LifeBuoy size={14} />使用说明 · 指标口径 · 常见问题</>}
      title="帮助中心"
      description="从发起一次挖掘，到读懂因子库与回测中心的每一个数字。"
    >
      <Panel title="上手流程">
        <div className="flow-row">
          {FLOW.map((step, index) => <Fragment key={step.title}>
            <button type="button" className="flow-step" onClick={() => navigate(step.to)} title={step.desc}>
              <span className="flow-badge" style={{ color: step.color, background: `${step.color}14` }}>{step.tag}</span>
              <span className="flow-icon" style={{ color: step.color, background: `${step.color}12`, borderColor: `${step.color}26` }}>{step.icon}</span>
              <strong>{step.title}</strong>
              <span className="flow-desc">{step.desc}</span>
            </button>
            {index < FLOW.length - 1 && <span className="flow-arrow" aria-hidden="true">→</span>}
          </Fragment>)}
        </div>
      </Panel>

      <Panel title="挖掘算法：两种，各自消费什么">
        <Glossary
          rows={[
            ['RD-Agent（默认）', 'LLM 多轮演化：假设 → 实验 → 编码 → 回测 → 反馈逐轮递进', '消费研究方向文本、演化轮数（loop_n）与数据集选择；需要配置对话模型'],
            ['统计量价挖掘', '按固定候选目录逐日检验量价因子，不调用 LLM', '不消费方向文本/数据集/loop_n（传了会显式报错，而不是静默忽略）'],
          ]}
        />
        <p className="subtle">两者都产出可回测的因子表达式；混用不会互相污染，任务行里记录了当时用的算法。</p>
      </Panel>

      <Panel title="任务中心 · 决策轨迹怎么读">
        <p>决策轨迹是一张只读归档表：<strong>每行 = 一轮演化（Loop_N）</strong>，<strong>每列 = 一个环节</strong>，顺序固定为：</p>
        <SpecPanel icon={<FlaskConical size={16} />} title="五环节" columns={5} items={[
          ['假设', 'hypothesis', 'LLM 按方向提出因子假设', '#2563eb'],
          ['实验', 'experiment', '把假设落成因子定义/公式', '#7c3aed'],
          ['编码', 'coding', 'CoSTEER 写因子代码并自测评审', '#0891b2'],
          ['回测', 'running', '跑回测拿 IC / 分层', '#f59e0b'],
          ['反馈', 'feedback', '生成本轮反馈，喂给下一轮', '#059669'],
        ]} />
        <p>格子里是该环节的<strong>事实状态</strong>：</p>
        <p><Status value="ok" /> 有产物且时长可解（回测环节无独立产物，有时长即正常）　<Status value="partial" /> 有证据但没跑完　<Status value="absent" /> 这个环节没跑到</p>
        <p className="subtle">轮次下方若出现「本轮标记」，只标位置与事实、不做因果推断：<strong>本循环产出因子均未进入实现</strong>（no_selected）、<strong>任务失败——本循环为最深到达</strong>（task_failed_here）、<strong>本循环产出因子结局均为被拒</strong>（all_rejected，多为与池内因子重复）。</p>
      </Panel>

      <Panel title="因子库 · 三个指标的口径">
        <Glossary
          rows={[
            ['IC', '日度截面 IC 的均值', 'Spearman 秩相关（不是 Pearson）；越接近 0 越弱，好因子通常 > 0.03'],
            ['Rank IC', '日度截面 IC 的中位数', '同样是秩相关；中位数比均值稳健，受极端交易日影响小'],
            ['ICIR', 'IC 均值 ÷ IC 标准差', '衡量预测力的稳定性；本框架核心择优依据，好因子通常 > 0.3'],
            ['夏普 / 年化收益', '多头组合的绩效', '见下方「回测中心」——含市场 beta，别当成因子 alpha'],
            ['年化换手', '日双边换手 Σ|Δw| 的年化', '单位是「倍」：78.9 表示一年换手约 7894%'],
          ]}
        />
        <p className="subtle">指标若来自回测台账而非因子行本身，详情页会标注「指标来自回测台账」——新回测链按跨市场纪律不回写因子行（避免美股/港股结果覆盖 A 股因子指标），读取时按最近一次 <strong>A 股</strong>完成回测兜底。</p>
      </Panel>

      <Panel title="因子库 · 生命周期：实验 → 观察 → 在用 → 退役">
        <Glossary
          rows={[
            ['实验', '已产出、尚无成功回测', '新挖出来的因子初始态；有活跃验证时展示为「验证中」'],
            ['观察', '已有成功回测，仍在入库后 180 天盲窗内', '盲窗内评估读数对研究员不可见，避免拿样本外结果反推'],
            ['在用', '盲窗到期，滚动 12 个月表现达标', '可正常用于组合与研究'],
            ['退役', '滚动 12m ICIR 连续 3 个月末低于 0.2', '降权不删行；恢复需人工'],
          ]}
        />
        <p className="subtle">注意「状态」列展示的是<strong>生命周期成熟度</strong>；失败 / 回测中 / 已取消等<strong>回测处理态</strong>会额外补一个标签。</p>
      </Panel>

      <Panel title="回测中心 · 口径与图表含义">
        <SpecPanel icon={<BarChart3 size={16} />} title="统一口径" note="A 股 · 日频" items={[
          ['多头组合', '每日截面 rank 前 30% 等权', '因子的评价基准口径', '#2563eb'],
          ['交易成本', '双边 cost_bps（默认 20）', '按日双边换手 Σ|Δw| 计费', '#f59e0b'],
          ['基准', '沪深 300（指数日线）', '读数失败时才降级为等权并在结果里标注', '#059669'],
          ['频率', '日频', '对齐后按 t 日因子预测 t→t+1 收益（无前视）', '#7c3aed'],
        ]} />
        <Glossary
          rows={[
            ['收益曲线', '基准 / 因子 / 超额 三条累计收益', '因子 = 多头组合；超额 = 因子 ÷ 基准 − 1；三条都从 0% 起'],
            ['IC 序列', '每日截面 IC', '红柱 = 正 IC，绿柱 = 负 IC（A 股红正绿负口径）'],
            ['分位组合', '按因子排名分 5 组各自的净值', 'Q1 最低分 … Q5 最高分；单调性越好说明因子分层能力越强'],
            ['换手率', '每日双边换手 Σ|Δw|', '成本敏感度直接看它'],
          ]}
        />
        <p className="subtle">扣费口径：扣费年化 = 毛年化 − 年化换手 × 成本；扣费夏普同理按净收益序列重算。报告可用右上角「导出 PDF 报告」留档。</p>
      </Panel>

      <Panel title="常见问题">
        <Glossary
          rows={[
            ['为什么 IC 很小、夏普却很高？', '两者不是一回事', '夏普/年化收益是「多头组合」口径，含市场 beta（同期基准也有夏普）；判断因子优劣要看 IC/ICIR 与多空腿，别只看夏普'],
            ['「年化换手 78.9」是 79% 吗？', '不是，单位是「倍」', '78.9 = 一年双边换手约 7894%；成本拖累 = 78.9 × 成本费率'],
            ['为什么有的因子没有 IC 数字？', '还没有可用的 A 股完成回测', '去回测中心给该因子跑一次；或该因子仅跑过非 A 股市场（跨市场结果不冒充 A 股指标）'],
            ['某张图是空的？', '该 run 没有对应序列数据', '例如回测未产出 IC 序列、或尚未生成分位/换手载荷；先看该 run 的状态与报告'],
            ['为什么产出因子变少了？', '高相关因子会被值级查重拒绝', '与池内已有因子 |ρ| ≥ 0.5 直接拒绝（0.4–0.5 灰区告警）——这是正交性硬指标'],
          ]}
        />
      </Panel>

      <Panel title="还需要更多？">
        <p>这份帮助覆盖本独立部署（因子挖掘切片）的全部可用页面。页面上的「查看完整轨迹 / 查看实验参数与数据版本 / 查看完整研究证据」等折叠块都保留了原始 JSON，是对账口径的第一手证据。</p>
        <p className="subtle"><BookOpen size={13} /> 技术性说明（模块边界、数据目录、验证脚本）见仓库根 <code>AGENTS.md</code> 与 <code>FACTOR_MINING_MIGRATION.md</code>。</p>
      </Panel>
    </Page>
  );
}
