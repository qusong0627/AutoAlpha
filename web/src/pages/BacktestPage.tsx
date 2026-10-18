import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Button, Card, Descriptions, Input, Select, message } from 'antd';
import {
  cancelBacktest,
  getBacktestStatus,
  listFactorLibraries,
  startBacktest,
  type FactorLibraryOption,
} from '../features/alpha-research/services-v2/api';
import { GradientProgress, PageHeader, StatCard, StatusBadge } from './ui';

function RangeInputs(props: {
  startDate: string;
  endDate: string;
  onStart: (v: string) => void;
  onEnd: (v: string) => void;
}) {
  return (
    <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
      <Input
        style={{ width: 130 }}
        placeholder="开始 2024-01-01"
        value={props.startDate}
        onChange={(e) => props.onStart(e.target.value)}
      />
      <span style={{ color: '#98a2b3' }}>~</span>
      <Input
        style={{ width: 130 }}
        placeholder="结束 2024-12-31"
        value={props.endDate}
        onChange={(e) => props.onEnd(e.target.value)}
      />
    </span>
  );
}

export default function BacktestPage() {
  const [params] = useSearchParams();
  const [options, setOptions] = useState<FactorLibraryOption[]>([]);
  const [factorId, setFactorId] = useState<string | undefined>(params.get('factorId') || undefined);
  const [universe, setUniverse] = useState<string | undefined>(undefined);
  const [startDate, setStartDate] = useState('');
  const [endDate, setEndDate] = useState('');
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState('');
  const [metrics, setMetrics] = useState<Record<string, number>>({});
  const [error, setError] = useState<string | undefined>(undefined);

  useEffect(() => {
    listFactorLibraries().then((r) => {
      if (r.success) setOptions(r.data.libraries);
    });
  }, []);

  useEffect(() => {
    const fid = params.get('factorId');
    if (fid) {
      setFactorId(fid);
      refresh(fid);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [params]);

  const refresh = async (fid: string) => {
    try {
      const r = await getBacktestStatus(fid);
      if (!r.success) return 'failed';
      setStatus(r.data.task.status);
      setMetrics((r.data.task as any).metrics ?? {});
      setError(r.data.error);
      return r.data.task.status;
    } catch {
      return 'failed';
    }
  };

  useEffect(() => {
    if (!factorId || !running) return;
    let stop = false;
    let timer = 0;
    const tick = async () => {
      const st = await refresh(factorId);
      if (!stop && st !== 'completed' && st !== 'failed') {
        timer = window.setTimeout(tick, 3000);
      } else {
        setRunning(false);
      }
    };
    timer = window.setTimeout(tick, 1000);
    return () => {
      stop = true;
      window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [running, factorId]);

  const submit = async () => {
    if (!factorId) {
      message.warning('请先选择因子');
      return;
    }
    setRunning(true);
    setError(undefined);
    try {
      const r = await startBacktest({
        factorId,
        universe,
        startDate: startDate.trim() || undefined,
        endDate: endDate.trim() || undefined,
      });
      if (!r.success) {
        message.error(r.error || '发起失败');
        setRunning(false);
      }
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '发起失败');
      setRunning(false);
    }
  };

  const cancel = async () => {
    if (!factorId) return;
    await cancelBacktest(factorId);
    message.info('已发送取消');
  };

  const m = metrics as Record<string, number | undefined>;
  const num = (v: number | undefined) =>
    typeof v === 'number' && Number.isFinite(v) ? v : undefined;
  const fmt = (v: number | undefined, d = 4) => (v === undefined ? '—' : v.toFixed(d));
  const tone = (v: number | undefined) =>
    v === undefined ? '' : v > 0 ? 'text-rose-500' : v < 0 ? 'text-emerald-500' : '';

  const opt = options.find((o) => o.id === factorId);

  return (
    <div className="qm-page">
      <PageHeader title="因子回测" desc="单因子 · Qlib 引擎" icon="◉" />

      <div className="qa-glass animate-fade-in-up" style={{ borderRadius: 16, padding: 16, marginBottom: 16 }}>
        <div className="qm-filter-row" style={{ marginBottom: 0 }}>
          <div className="grow">
            <Select
              showSearch
              style={{ width: '100%' }}
              placeholder="选择因子"
              value={factorId}
              onChange={(v) => {
                setFactorId(v);
                setMetrics({});
                setError(undefined);
                setStatus('');
              }}
              filterOption={(input, o) =>
                String(o?.label ?? '').toLowerCase().includes(input.toLowerCase())
              }
              options={options.map((o) => ({
                value: o.id,
                label: `${o.name}（${o.id.slice(0, 8)}${o.ic != null ? `, IC ${o.ic.toFixed(4)}` : ''}）`,
              }))}
            />
          </div>
          <Select
            style={{ width: 140 }}
            placeholder="股票池"
            allowClear
            value={universe}
            onChange={setUniverse}
            options={['csi300', 'csi500', 'sse50'].map((u) => ({ value: u, label: u }))}
          />
          <RangeInputs
            startDate={startDate}
            endDate={endDate}
            onStart={setStartDate}
            onEnd={setEndDate}
          />
          <Button type="primary" shape="round" loading={running} onClick={submit}>
            开始回测
          </Button>
          {running && <Button onClick={cancel}>取消</Button>}
        </div>
      </div>

      {factorId && (
        <Card
          title={
            <span>
              {opt?.name ?? factorId.slice(0, 12)} <StatusBadge status={status || 'idle'} />
            </span>
          }
          style={{ borderRadius: 12 }}
        >
          {running && <GradientProgress percent={50} />}
          {error && <p style={{ color: '#e11d48' }}>{error}</p>}
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, margin: '12px 0' }}>
            <StatCard label="IC" value={fmt(num(m.ic))} tone={tone(num(m.ic))} />
            <StatCard label="RankIC" value={fmt(num(m.rankIc))} tone={tone(num(m.rankIc))} />
            <StatCard label="ICIR" value={fmt(num(m.icir), 2)} tone={tone(num(m.icir))} />
            <StatCard label="年化收益" value={m.annualReturn === undefined ? '—' : `${(m.annualReturn * 100).toFixed(2)}%`} tone={tone(num(m.annualReturn))} />
          </div>
          <Descriptions column={3} bordered size="small">
            <Descriptions.Item label="夏普">{fmt(num(m.sharpeRatio), 2)}</Descriptions.Item>
            <Descriptions.Item label="最大回撤">
              {m.maxDrawdown === undefined ? '—' : `${(m.maxDrawdown * 100).toFixed(2)}%`}
            </Descriptions.Item>
            <Descriptions.Item label="RRE">{fmt(num(m.rre), 2)}</Descriptions.Item>
            <Descriptions.Item label="PFS">{fmt(num(m.pfs), 2)}</Descriptions.Item>
            <Descriptions.Item label="年化换手">
              {m.annTurnover === undefined ? '—' : `${(m.annTurnover * 100).toFixed(2)}%`}
            </Descriptions.Item>
            <Descriptions.Item label="扣费年化">
              {m.annReturnNet === undefined ? '—' : `${(m.annReturnNet * 100).toFixed(2)}%`}
            </Descriptions.Item>
            <Descriptions.Item label="观测数">
              {m.nObs === undefined ? '—' : String(Math.round(m.nObs))}
            </Descriptions.Item>
          </Descriptions>
        </Card>
      )}
    </div>
  );
}
