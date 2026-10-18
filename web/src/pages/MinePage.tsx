import { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Button, Input, Select, Table, message } from 'antd';
import {
  cancelMining,
  getDataSummary,
  getMiningStatus,
  getTaskLog,
  getUniverses,
  listTasks,
  startMining,
} from '../features/alpha-research/services-v2/api';
import type { DataSummary, Task, UniverseInfo } from '../features/alpha-research/types-v2';
import {
  getMiningAlgorithms,
  selectableAlgorithms,
  type MiningAlgorithm,
} from '../features/alpha-research/services-v2/miningAlgorithmApi';
import { GradientProgress, LogView, StatCard, StatusBadge, ToneNum } from './ui';

const PRESET_UNIVERSE_KEY = 'qm.preset.universe';
const PRESET_ALGO_KEY = 'qm.preset.algorithm';
const PRESET_PROMPTS = [
  '挖掘基于5日动量反转与成交量价偏离组合的超额收益因子',
  '构建捕捉日内高频波动非对称性与价格跳跃的量价特征',
  '寻找尾盘主力资金净流入与换手率背离的Alpha选股因子',
  '基于多周期均线发散度与流动性溢价挖掘中短线稳健因子',
];
const MARKETS = [
  { key: 'a_share', label: 'A股', enabled: true },
  { key: 'futures', label: '期货', enabled: false },
  { key: 'hong_kong', label: '港股', enabled: false },
  { key: 'us_stock', label: '美股', enabled: false },
];

export default function MinePage() {
  const navigate = useNavigate();
  const [direction, setDirection] = useState('');
  const [universe, setUniverse] = useState(
    () => localStorage.getItem(PRESET_UNIVERSE_KEY) || 'csi300',
  );
  const [universeInfos, setUniverseInfos] = useState<UniverseInfo[]>([]);
  const [algoKey, setAlgoKey] = useState(
    () => localStorage.getItem(PRESET_ALGO_KEY) || 'statistical',
  );
  const [algos, setAlgos] = useState<MiningAlgorithm[]>([]);
  const [dataSource, setDataSource] = useState('qlib_bin');
  const [backendOk, setBackendOk] = useState<boolean | null>(null);
  const [summary, setSummary] = useState<DataSummary | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [task, setTask] = useState<Task | null>(null);
  const [logs, setLogs] = useState<string[]>([]);
  const [recent, setRecent] = useState<Task[]>([]);

  useEffect(() => {
    getMiningAlgorithms().then((r) => {
      setBackendOk(r.success);
      if (r.success) setAlgos(selectableAlgorithms(r.data));
    });
    getUniverses().then((r) => {
      if (r.success) setUniverseInfos(r.data.universes);
    });
    getDataSummary().then((r) => {
      if (r.success) setSummary(r.data);
    });
    listTasks().then((r) => {
      if (r.success) setRecent(r.data.tasks.slice(0, 8));
    });
  }, []);

  const algo = algos.find((a) => a.key === algoKey) ?? null;
  const needsDirection = algo ? algo.usesLlm : algoKey !== 'statistical';

  const poll = useCallback(async (id: string) => {
    try {
      const r = await getMiningStatus(id);
      if (r.success) setTask(r.data.task);
      const lg = await getTaskLog(id);
      setLogs(lg.lines);
      return r.success ? r.data.task.status : '';
    } catch {
      return '';
    }
  }, []);

  useEffect(() => {
    if (!taskId) return;
    let stop = false;
    const tick = async () => {
      const st = await poll(taskId);
      if (!stop && st !== 'completed' && st !== 'failed') {
        timer = window.setTimeout(tick, 2000);
      }
    };
    let timer = window.setTimeout(tick, 0);
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
  }, [taskId, poll]);

  const submit = async () => {
    if (needsDirection && !direction.trim()) {
      message.warning('请描述要挖掘的因子方向');
      return;
    }
    setSubmitting(true);
    try {
      const r = await startMining({
        direction: needsDirection ? direction.trim() : '',
        market: 'a_share',
        universe,
        dataSource,
        algorithm: algoKey || undefined,
      });
      if (!r.success) {
        message.error(r.error || '提交失败');
        return;
      }
      if (!r.data.taskId) {
        message.error('后端未返回任务 id');
        return;
      }
      localStorage.setItem(PRESET_UNIVERSE_KEY, universe);
      localStorage.setItem(PRESET_ALGO_KEY, algoKey);
      setTaskId(r.data.taskId);
      setTask(r.data.task ?? null);
      setLogs([]);
      message.success(`任务已启动：${r.data.taskId}`);
      const lt = await listTasks();
      if (lt.success) setRecent(lt.data.tasks.slice(0, 8));
      document.getElementById('qm-task')?.scrollIntoView({ behavior: 'smooth' });
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '提交失败');
    } finally {
      setSubmitting(false);
    }
  };

  const factors = task?.factors ?? [];
  const withIc = factors.filter((f) => typeof f.ic === 'number');
  const bestRankIc = withIc.reduce((m, f) => Math.max(m, Math.abs(f.rankIc ?? 0)), 0);
  const pct = task?.progress?.progress ?? 0;
  const running = task?.status === 'running' || task?.status === 'queued';
  const dr = summary?.dateRange;
  const l1cols = summary?.datasets?.l1_factors?.columns;

  const uniLabel = (id: string) => {
    const u = universeInfos.find((x) => x.id === id);
    return u ? `${u.name}${u.stockCount ? ` (${u.stockCount})` : ''}` : id;
  };

  return (
    <div style={{ maxWidth: 1100, margin: '0 auto', padding: '28px 24px 48px' }}>
      {/* Hero */}
      <div style={{ textAlign: 'center', marginBottom: 20 }}>
        <div
          style={{
            display: 'inline-flex',
            alignItems: 'center',
            gap: 8,
            background: '#fff',
            border: '1px solid #e8ecf3',
            borderRadius: 9999,
            padding: '5px 14px',
            fontSize: 12,
            fontWeight: 700,
            color: '#2f6bff',
            marginBottom: 12,
          }}
        >
          ⚡ LLM 驱动自主量化因子演化平台
          <span style={{ color: '#98a2b3' }}>● AutoAlpha2.0</span>
        </div>
        <h1 style={{ margin: 0, fontSize: 34, fontWeight: 800, color: '#1f2733' }}>
          欢迎使用 AutoAlpha
        </h1>
        <p style={{ color: '#5b6675', fontSize: 14, margin: '8px 0 10px' }}>
          用自然语言描述量化假设，AI 自动生成表达式、因子特征、样本内挖掘与进化回测
        </p>
        {backendOk === null ? null : backendOk ? (
          <span
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: '#059669',
              background: '#ecfdf5',
              borderRadius: 9999,
              padding: '4px 14px',
            }}
          >
            已连接后端服务
          </span>
        ) : (
          <span
            style={{
              fontSize: 12,
              fontWeight: 700,
              color: '#e11d48',
              background: '#fff1f2',
              borderRadius: 9999,
              padding: '4px 14px',
            }}
          >
            后端不可达
          </span>
        )}
      </div>

      {/* 输入卡 */}
      <div
        className="qa-glass animate-fade-in-up"
        style={{ borderRadius: 16, padding: '14px 18px', marginBottom: 12 }}
      >
        <div
          style={{
            display: 'flex',
            gap: 14,
            alignItems: 'center',
            flexWrap: 'wrap',
            fontSize: 12,
            color: '#5b6675',
            paddingBottom: 10,
            borderBottom: '1px solid #eef1f6',
            marginBottom: 10,
          }}
        >
          <span>
            市场{' '}
            {MARKETS.map((m) => (
              <button
                key={m.key}
                disabled={!m.enabled}
                onClick={() => undefined}
                title={m.enabled ? '' : '暂只支持 A 股'}
                style={{
                  border: '1px solid ' + (m.enabled ? '#bfdbfe' : 'transparent'),
                  background: m.enabled ? '#eff6ff' : 'transparent',
                  color: m.enabled ? '#2563eb' : '#cbd5e1',
                  borderRadius: 9999,
                  padding: '2px 10px',
                  fontSize: 12,
                  fontWeight: 700,
                  marginLeft: 4,
                  cursor: m.enabled ? 'default' : 'not-allowed',
                }}
              >
                {m.enabled ? '● ' : '○ '}{m.label}
              </button>
            ))}
          </span>
          <span>
            池{' '}
            <Select
              variant="borderless"
              style={{ width: 170, fontWeight: 700 }}
              value={universe}
              onChange={setUniverse}
              options={(universeInfos.length > 0
                ? universeInfos.map((u) => u.id)
                : ['csi300', 'csi500', 'csi1000', 'sse50', 'all_a']
              ).map((id) => ({ value: id, label: uniLabel(id) }))}
            />
          </span>
          <span>
            数据{' '}
            {['qlib_bin', 'parquet'].map((d) => (
              <button
                key={d}
                onClick={() => setDataSource(d)}
                style={{
                  border: '1px solid ' + (dataSource === d ? '#bfdbfe' : 'transparent'),
                  background: dataSource === d ? '#eff6ff' : 'transparent',
                  color: dataSource === d ? '#2563eb' : '#64748b',
                  borderRadius: 9999,
                  padding: '2px 10px',
                  fontSize: 12,
                  fontWeight: 700,
                  marginLeft: 4,
                  cursor: 'pointer',
                }}
              >
                {d === 'qlib_bin' ? 'Qlib' : 'Parquet'}
              </button>
            ))}
          </span>
          <span>
            ◎ 方向
            <Select
              variant="borderless"
              style={{ width: 190, fontWeight: 700 }}
              value={algoKey}
              onChange={setAlgoKey}
              options={
                algos.length > 0
                  ? algos.map((a) => ({ value: a.key, label: a.name }))
                  : [
                      { value: 'statistical', label: '统计量价挖掘' },
                      { value: 'rdagent', label: 'RD-Agent 因子演化' },
                    ]
              }
            />
          </span>
        </div>
        <div style={{ display: 'flex', gap: 10 }}>
          <Input.TextArea
            rows={3}
            variant="borderless"
            style={{ fontSize: 13, background: '#f8fafc', borderRadius: 12 }}
            placeholder="描述因子挖掘需求（如：挖掘基于5日动量反转与成交量价偏离组合的Alpha因子），按 Enter 发送"
            value={direction}
            onChange={(e) => setDirection(e.target.value)}
            onPressEnter={(e) => {
              if (!e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
          />
          <Button
            type="primary"
            shape="circle"
            size="large"
            loading={submitting}
            onClick={submit}
            style={{ alignSelf: 'flex-end', width: 44, height: 44, fontSize: 18 }}
          >
            ➤
          </Button>
        </div>
      </div>

      {/* 推荐方向 */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 20, alignItems: 'center' }}>
        <span style={{ fontSize: 12, color: '#d97706', fontWeight: 700 }}>✨ 推荐方向：</span>
        {PRESET_PROMPTS.map((p) => (
          <button
            key={p}
            onClick={() => setDirection(p)}
            style={{
              background: '#fff',
              border: '1px solid #e8ecf3',
              borderRadius: 9999,
              padding: '5px 14px',
              fontSize: 12,
              color: '#475569',
              cursor: 'pointer',
            }}
          >
            {p}
          </button>
        ))}
      </div>

      {/* 三卡片 */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 16, marginBottom: 16 }}>
        {[
          {
            icon: '🤖',
            tag: '演化台',
            tagColor: '#2563eb',
            title: 'AI 因子挖掘',
            desc: 'LLM 自动理解需求，生成因子假设并执行多迭代遗传演化',
            link: '进入实时演化台 →',
            onClick: () => document.getElementById('qm-task')?.scrollIntoView({ behavior: 'smooth' }),
          },
          {
            icon: '🗄',
            tag: '全量库',
            tagColor: '#059669',
            title: '因子库管理',
            desc: '浏览、筛选、分析已挖掘的所有因子及其 IC/IR 与多空收益单调性',
            link: '查看因子资产库 →',
            onClick: () => navigate('/factors'),
          },
          {
            icon: '📊',
            tag: '样本外验证',
            tagColor: '#a855f7',
            title: '全周期回测',
            desc: '选择已生成的因子执行全市场、全周期样本外回测评估',
            link: '启动独立回测 →',
            onClick: () => navigate('/backtest'),
          },
        ].map((c) => (
          <div key={c.title} className="qa-glass" style={{ borderRadius: 12, padding: 20, textAlign: 'center' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
              <span style={{ fontSize: 22 }}>{c.icon}</span>
              <span style={{ fontSize: 11, fontWeight: 700, color: c.tagColor }}>{c.tag}</span>
            </div>
            <div style={{ fontWeight: 800, fontSize: 15, margin: '8px 0' }}>{c.title}</div>
            <div style={{ fontSize: 12, color: '#64748b', minHeight: 48 }}>{c.desc}</div>
            <button
              onClick={c.onClick}
              style={{ background: 'none', border: 'none', color: c.tagColor, fontWeight: 700, fontSize: 13, cursor: 'pointer', marginTop: 8 }}
            >
              {c.link}
            </button>
          </div>
        ))}
      </div>

      {/* 系统环境与数据规格 */}
      <div className="qa-glass" style={{ borderRadius: 12, padding: 20, marginBottom: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
          <span style={{ color: '#2563eb' }}>🛡</span>
          <b style={{ fontSize: 14 }}>系统投研环境与数据规格</b>
          <span style={{ marginLeft: 'auto', fontSize: 12, color: '#98a2b3' }}>
            Qlib Alpha Engine · Ready
          </span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12 }}>
          <div style={{ border: '1px solid #eef1f6', borderRadius: 10, padding: 14, textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: '#2563eb', fontWeight: 700 }}>⛁ 股票池覆盖</div>
            <div style={{ fontWeight: 800, fontSize: 13, margin: '6px 0' }}>
              {universeInfos.length ? `${universeInfos.length} 个 A 股可选池` : 'A 股可选池'}
            </div>
            <div style={{ fontSize: 11, color: '#98a2b3' }}>A 股 · 日线/分钟</div>
          </div>
          <div style={{ border: '1px solid #eef1f6', borderRadius: 10, padding: 14, textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: '#059669', fontWeight: 700 }}>▤ 基础特征集</div>
            <div style={{ fontWeight: 800, fontSize: 13, margin: '6px 0' }}>
              {l1cols ? `QuantDB L1（${l1cols} 列）` : 'QuantDB L1 因子库'}
            </div>
            <div style={{ fontSize: 11, color: '#98a2b3' }}>动量/波动/流动性等多类因子库</div>
          </div>
          <div style={{ border: '1px solid #eef1f6', borderRadius: 10, padding: 14, textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: '#7c3aed', fontWeight: 700 }}>〜 训练与验证跨度</div>
            <div style={{ fontWeight: 800, fontSize: 13, margin: '6px 0' }}>
              {dr ? `${dr.start} ~ ${dr.end}` : '—'}
            </div>
            <div style={{ fontSize: 11, color: '#98a2b3' }}>
              {dr ? `初步初筛在样本外检验单独执行` : '—'}
            </div>
          </div>
          <div style={{ border: '1px solid #eef1f6', borderRadius: 10, padding: 14, textAlign: 'center' }}>
            <div style={{ fontSize: 11, color: '#a855f7', fontWeight: 700 }}>❖ 并行演化架构</div>
            <div style={{ fontWeight: 800, fontSize: 13, margin: '6px 0' }}>多轮迭代·多方向并行</div>
            <div style={{ fontSize: 11, color: '#98a2b3' }}>消耗与（进化轮次 × 方向）成正比</div>
          </div>
        </div>
      </div>

      {/* 实时演化台 */}
      <div id="qm-task" className="qa-glass" style={{ borderRadius: 16, padding: 16, marginBottom: 16 }}>
        <div style={{ fontWeight: 800, fontSize: 14, marginBottom: 8 }}>实时演化台</div>
        {recent.length > 0 && (
          <div style={{ display: 'flex', gap: 8, marginBottom: 12, flexWrap: 'wrap' }}>
            {recent.map((t) => (
              <button
                key={t.taskId}
                onClick={() => {
                  setTaskId(t.taskId);
                  setTask(null);
                  setLogs([]);
                }}
                style={{
                  borderRadius: 9999,
                  border: `1px solid ${t.taskId === taskId ? '#6366f1' : '#e2e8f0'}`,
                  background: t.taskId === taskId ? '#eef2ff' : '#fff',
                  padding: '4px 12px',
                  fontSize: 11,
                  fontWeight: 700,
                  cursor: 'pointer',
                  color: '#475569',
                }}
              >
                <span
                  style={{
                    display: 'inline-block',
                    width: 6,
                    height: 6,
                    borderRadius: 9999,
                    marginRight: 6,
                    background:
                      t.status === 'running'
                        ? '#3b82f6'
                        : t.status === 'completed'
                          ? '#10b981'
                          : t.status === 'failed'
                            ? '#f43f5e'
                            : '#94a3b8',
                  }}
                />
                {t.taskId.slice(0, 8)} · {Math.round(t.progress?.progress ?? 0)}%
              </button>
            ))}
          </div>
        )}
        {taskId ? (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
              <span className="qm-factor-id">{taskId}</span>
              {task && <StatusBadge status={task.status} />}
              <span style={{ fontSize: 12, color: '#64748b' }}>
                {task?.progress?.message || '等待状态…'}
              </span>
              <span style={{ flex: 1 }} />
              {(task?.status === 'running' || task?.status === 'queued') && (
                <Button
                  size="small"
                  onClick={async () => {
                    await cancelMining(taskId);
                    message.info('已发送取消');
                    poll(taskId);
                  }}
                >
                  取消
                </Button>
              )}
            </div>
            <GradientProgress percent={task?.progress?.progress ?? 0} failed={task?.status === 'failed'} />
            {(task?.factors ?? []).length > 0 && (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, margin: '12px 0' }}>
                <StatCard label="产出因子" value={String((task?.factors ?? []).length)} />
                <StatCard label="有 IC" value={String(withIc.length)} />
                <StatCard
                  label="最佳 RankIC"
                  value={withIc.length ? bestRankIc.toFixed(4) : '—'}
                />
                <StatCard label="进度" value={`${Math.round(task?.progress?.progress ?? 0)}%`} />
              </div>
            )}
            <LogView lines={logs} height={320} />
          </div>
        ) : (
          <div style={{ color: '#98a2b3', fontSize: 13, textAlign: 'center', padding: '20px 0' }}>
            在上方描述需求并发送，演化日志将实时显示在这里
          </div>
        )}
      </div>

      {/* 最近任务表 */}
      <div className="qm-card">
        <div style={{ fontWeight: 700, marginBottom: 8, fontSize: 14 }}>最近任务</div>
        <Table
          size="small"
          rowKey="taskId"
          pagination={false}
          dataSource={recent}
          onRow={(r) => ({
            onClick: () => {
              setTaskId(r.taskId);
              setTask(null);
              setLogs([]);
              document.getElementById('qm-task')?.scrollIntoView({ behavior: 'smooth' });
            },
            style: { cursor: 'pointer' },
          })}
          columns={[
            {
              title: '任务',
              dataIndex: 'taskId',
              render: (v: string) => <span className="qm-factor-id">{String(v).slice(0, 12)}</span>,
            },
            {
              title: '方向',
              dataIndex: ['config', 'userInput'],
              ellipsis: true,
              render: (v: string) => v || '—',
            },
            { title: '状态', dataIndex: 'status', render: (v: string) => <StatusBadge status={v} /> },
            {
              title: '进度',
              dataIndex: ['progress', 'progress'],
              render: (v: number) => <span className="qm-num">{Math.round(v ?? 0)}%</span>,
            },
            {
              title: '最佳 RankIC',
              render: (_: unknown, t: Task) => {
                const fs = t.factors ?? [];
                const b = fs.reduce((m, f) => Math.max(m, Math.abs(f.rankIc ?? 0)), 0);
                return <ToneNum value={fs.length ? b : undefined} />;
              },
            },
          ]}
        />
      </div>
    </div>
  );
}
