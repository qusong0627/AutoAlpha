import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { BarChart3, Bot, ChevronDown, Database, Sparkles, Wand2 } from 'lucide-react';
import MiningForm, { initialDraft, type MiningDraft } from './MiningForm';
import MiningInsights from './MiningInsights';
import { useWorkspace } from './WorkspaceContext';
import { CardGrid, Page } from './ui';

export default function HomePage() {
  const navigate = useNavigate();
  const overview = useWorkspace();
  const summary = overview.summary.data;
  // 方向助手（把一个方向拆成多张假设卡并行派发 / 经验记忆与饱和方向 / 拒因）需要
  // 上方输入框里的方向——只有首页有方向输入，所以它挂在这里；默认收起，落地页保持轻。
  const [draft, setDraft] = useState<MiningDraft>(() => initialDraft());
  const [showInsights, setShowInsights] = useState(false);
  return (
    <Page
      className="workspace-home-page"
      eyebrow={<><Sparkles size={14} />LLM 驱动自主量化因子演化平台 · AutoAlpha</>}
      title="欢迎使用 AutoAlpha"
      description="用自然语言描述量化假设，AI 自动生成表达式、因子特征、样本内挖掘与进化回测"
      actions={
        <span className={`status-chip ${summary?.available ? 'online' : ''}`}>
          <i className={`status-dot ${summary?.available ? 'online' : 'offline'}`} />
          {summary ? (summary.available ? '已连接后端服务' : summary.error || '数据尚未就绪') : '正在检查数据源'}
        </span>
      }
    >
      <MiningForm onDraftChange={setDraft} />
      <div className="insights-toggle">
        <button type="button" aria-expanded={showInsights} onClick={() => setShowInsights(value => !value)}>
          <Wand2 size={13} />
          {showInsights ? '收起方向助手' : '方向助手：拆成多个方向并行挖掘 · 看经验与拒因'}
          <ChevronDown size={13} className={showInsights ? 'flipped' : ''} />
        </button>
      </div>
      {showInsights && <MiningInsights draft={draft} />}
      <CardGrid
        columns={3}
        cards={[
          {
            icon: <Bot size={18} />,
            tag: '任务跟踪',
            color: '#2563eb',
            title: '任务中心',
            description: '查看任务进度、运行状态与挖掘结果',
            link: '查看任务 →',
            onClick: () => navigate('/tasks'),
          },
          {
            icon: <Database size={18} />,
            tag: '全量库',
            color: '#059669',
            title: '因子库管理',
            description: '浏览、筛选、分析已挖掘的所有因子及其 IC/IR 与多空收益单调性',
            link: '查看因子资产库 →',
            onClick: () => navigate('/factors'),
          },
          {
            icon: <BarChart3 size={18} />,
            tag: '样本外验证',
            color: '#9333ea',
            title: '全周期回测',
            description: '选择已生成的因子库执行全市场、全周期样本外回测评估',
            link: '启动独立回测 →',
            onClick: () => navigate('/backtest'),
          },
        ]}
      />
    </Page>
  );
}
