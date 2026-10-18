import { useEffect, useState } from 'react';
import { Button, Card, Descriptions, Select, Tag, message } from 'antd';
import { getDataSummary, listTasks } from '../features/alpha-research/services-v2/api';
import type { DataSummary } from '../features/alpha-research/types-v2';
import type { Task } from '../features/alpha-research/types-v2';
import { PageHeader, StatCard } from './ui';

const PRESET_UNIVERSE_KEY = 'qm.preset.universe';
const PRESET_ALGO_KEY = 'qm.preset.algorithm';
const UNIVERSES = ['csi300', 'csi500', 'csi1000', 'sse50', 'all_a'];

export default function MePage() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [summary, setSummary] = useState<DataSummary | null>(null);
  const [universe, setUniverse] = useState(
    () => localStorage.getItem(PRESET_UNIVERSE_KEY) || 'csi300',
  );
  const [algo, setAlgo] = useState(
    () => localStorage.getItem(PRESET_ALGO_KEY) || 'statistical',
  );

  useEffect(() => {
    listTasks().then((r) => {
      if (r.success) setTasks(r.data.tasks);
    });
    getDataSummary().then((r) => {
      if (r.success) setSummary(r.data);
    });
  }, []);

  const savePreset = () => {
    localStorage.setItem(PRESET_UNIVERSE_KEY, universe);
    localStorage.setItem(PRESET_ALGO_KEY, algo);
    message.success('挖掘预设已保存（挖掘页自动采用）');
  };

  const byStatus = (s: string) => tasks.filter((t) => t.status === s).length;
  const dr = summary?.dateRange;

  return (
    <div className="qm-page">
      <PageHeader title="个人中心" desc="本地部署 · 单用户" icon="☺" />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 16 }}>
        <StatCard label="累计任务" value={String(tasks.length)} />
        <StatCard label="已完成" value={String(byStatus('completed'))} />
        <StatCard label="进行中" value={String(byStatus('running') + byStatus('queued'))} />
        <StatCard label="失败" value={String(byStatus('failed'))} />
      </div>

      <div className="qm-card">
        <div style={{ fontWeight: 600, marginBottom: 8 }}>
          身份 <Tag color="blue" style={{ marginLeft: 8 }}>dev admin（免登录）</Tag>
        </div>
        <div style={{ color: '#5b6675', fontSize: 13 }}>
          后端 <span className="qm-factor-id">AUTH_DISABLED=1</span>，跳过登录直接注入管理员身份。
          对外暴露前需开启认证，此页届时再接用户体系。
        </div>
      </div>

      <div className="qm-card">
        <div style={{ fontWeight: 600, marginBottom: 8 }}>挖掘预设</div>
        <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
          <Select
            style={{ width: 160 }}
            value={universe}
            onChange={setUniverse}
            options={UNIVERSES.map((u) => ({ value: u, label: `股票池 · ${u}` }))}
          />
          <Select
            style={{ width: 220 }}
            value={algo}
            onChange={setAlgo}
            options={[
              { value: 'statistical', label: '统计量价挖掘' },
              { value: 'rdagent', label: 'RD-Agent 因子演化' },
            ]}
          />
          <Button type="primary" onClick={savePreset}>
            保存
          </Button>
        </div>
      </div>

      <Card title="数据覆盖" style={{ borderRadius: 12 }}>
        {!summary ? (
          <span style={{ color: '#98a2b3' }}>加载中…</span>
        ) : !summary.available ? (
          <span style={{ color: '#dc2626' }}>{summary.error || '数据不可用'}</span>
        ) : (
          <Descriptions column={2} size="small">
            <Descriptions.Item label="状态">可用</Descriptions.Item>
            <Descriptions.Item label="股票数">{summary.stockCount ?? '—'}</Descriptions.Item>
            <Descriptions.Item label="区间">
              {dr ? `${dr.start} ~ ${dr.end}（${dr.tradingDays} 交易日）` : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="数据集">
              {summary.datasets ? Object.keys(summary.datasets).length + ' 个' : '—'}
            </Descriptions.Item>
          </Descriptions>
        )}
      </Card>
    </div>
  );
}
