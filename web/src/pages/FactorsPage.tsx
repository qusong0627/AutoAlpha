import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Button, Drawer, Input, Modal, Select, Table, Tag, message } from 'antd';
import {
  explainFactor,
  exportFactorToIde,
  getFactorDetail,
  getFactors,
} from '../features/alpha-research/services-v2/api';
import type { Factor } from '../features/alpha-research/types-v2';
import { PageHeader, QualityBadge, StatCard, ToneNum } from './ui';

const UNIVERSES = ['csi300', 'csi500', 'csi1000', 'sse50', 'all_a'];

function numVal(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined;
}

export default function FactorsPage() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const [factors, setFactors] = useState<Factor[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [search, setSearch] = useState('');
  const [universe, setUniverse] = useState<string | undefined>(undefined);
  const [quality, setQuality] = useState<string | undefined>(undefined);
  const [detail, setDetail] = useState<any | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [explaining, setExplaining] = useState(false);

  const taskId = params.get('taskId') || undefined;

  const load = async () => {
    setLoading(true);
    try {
      const r = await getFactors({ limit: 200, universe, taskId });
      if (r.success) {
        setFactors(r.data.factors);
        setTotal(r.data.total);
      }
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [universe, taskId]);

  const filtered = useMemo(() => {
    let rows = factors;
    if (quality) rows = rows.filter((f) => f.quality === quality);
    const s = search.trim().toLowerCase();
    if (s) {
      rows = rows.filter(
        (f) =>
          f.factorName.toLowerCase().includes(s) ||
          f.factorId.toLowerCase().includes(s) ||
          (f.factorExpression || '').toLowerCase().includes(s),
      );
    }
    return rows;
  }, [factors, search, quality]);

  const withIc = factors.filter((f) => typeof f.ic === 'number').length;

  const openDetail = async (id: string) => {
    try {
      const r = await getFactorDetail(id);
      if (r.success) {
        setDetail(r.data.factor);
        setDrawerOpen(true);
      }
    } catch {
      message.error('详情加载失败');
    }
  };

  const explain = async (id: string) => {
    setExplaining(true);
    try {
      const r = await explainFactor(id);
      if (r.success) {
        Modal.info({ title: '因子解释', content: r.data.explanation || '（空）', width: 640 });
      } else {
        message.error(r.error || '解释失败');
      }
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '解释失败');
    } finally {
      setExplaining(false);
    }
  };

  const exportIt = async (id: string) => {
    try {
      const r = await exportFactorToIde(id);
      message.success(r.success ? r.data.message || '已导出' : r.error || '导出失败');
    } catch (e: any) {
      message.error(e?.response?.data?.detail || '导出失败');
    }
  };

  const meta = detail?.raw?.metadata ?? {};

  const groups: { title: string; rows: [string, number | undefined, number?][] }[] = detail
    ? [
        {
          title: '预测能力',
          rows: [
            ['IC', detail.ic],
            ['RankIC', detail.rankIc],
            ['ICIR', detail.icir],
            ['RankICIR', detail.rankIcir],
            ['观测数', detail.nObs, 0],
          ],
        },
        {
          title: '组合收益',
          rows: [
            ['年化收益', detail.annualReturn],
            ['夏普', detail.sharpeRatio],
            ['最大回撤', detail.maxDrawdown],
            ['扣费年化', detail.annReturnNet],
            ['扣费夏普', detail.sharpeNet],
          ],
        },
        {
          title: '稳健性',
          rows: [
            ['RRE', detail.rre],
            ['PFS', detail.pfsQuality?.pfs],
            ['PFS Gauss', detail.pfsQuality?.pfsGauss],
            ['PFS t', detail.pfsQuality?.pfsT],
          ],
        },
        {
          title: '交易属性',
          rows: [
            ['日换手', detail.turnoverDaily],
            ['年化换手', detail.annTurnover],
          ],
        },
      ]
    : [];

  return (
    <div className="qm-page">
      <PageHeader
        title="因子管理"
        desc={taskId ? `任务 ${taskId.slice(0, 8)} 的产出` : '全库因子 · 缺失显 —，真 0 显 0'}
        icon="◈"
      />

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4, 1fr)', gap: 12, marginBottom: 16 }}>
        <StatCard label="因子总数" value={String(total)} />
        <StatCard label="有 IC" value={String(withIc)} />
        <StatCard label="高质量" value={String(factors.filter((f) => f.quality === 'high').length)} />
        <StatCard label="当前筛选" value={String(filtered.length)} />
      </div>

      <div className="qm-card">
        <div className="qm-filter-row">
          <div className="grow">
            <Input.Search
              placeholder="搜索名称 / ID / 表达式"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              allowClear
            />
          </div>
          <Select
            style={{ width: 140 }}
            placeholder="股票池"
            allowClear
            value={universe}
            onChange={setUniverse}
            options={UNIVERSES.map((u) => ({ value: u, label: u }))}
          />
          <Select
            style={{ width: 120 }}
            placeholder="质量"
            allowClear
            value={quality}
            onChange={setQuality}
            options={[
              { value: 'high', label: '高' },
              { value: 'medium', label: '中' },
              { value: 'low', label: '低' },
            ]}
          />
          <Button onClick={load}>刷新</Button>
        </div>
        <Table
          size="middle"
          rowKey="factorId"
          loading={loading}
          dataSource={filtered}
          pagination={{ pageSize: 20, showSizeChanger: false }}
          columns={[
            {
              title: '因子',
              render: (_: unknown, f: Factor) => (
                <div>
                  <div className="qm-factor-name">
                    {f.factorName || '未命名'} <QualityBadge quality={f.quality} />
                  </div>
                  <div className="qm-factor-id">{f.factorId.slice(0, 12)}</div>
                  <div className="qm-factor-expr" title={f.factorExpression}>
                    {f.factorExpression || '—'}
                  </div>
                </div>
              ),
            },
            {
              title: 'RankIC',
              dataIndex: 'rankIc',
              width: 95,
              sorter: (a: Factor, b: Factor) => (numVal(a.rankIc) ?? -Infinity) - (numVal(b.rankIc) ?? -Infinity),
              render: (v: number) => <ToneNum value={numVal(v)} />,
            },
            {
              title: 'IC',
              dataIndex: 'ic',
              width: 95,
              sorter: (a: Factor, b: Factor) => (numVal(a.ic) ?? -Infinity) - (numVal(b.ic) ?? -Infinity),
              render: (v: number) => <ToneNum value={numVal(v)} />,
            },
            {
              title: 'ICIR',
              dataIndex: 'icir',
              width: 85,
              render: (v: number) => <ToneNum value={numVal(v)} decimals={2} />,
            },
            {
              title: '年化',
              dataIndex: 'annualReturn',
              width: 90,
              render: (v: number) => {
                const n = numVal(v);
                if (n === undefined) return <span style={{ color: '#98a2b3' }}>—</span>;
                return <ToneNum value={n * 100} decimals={2} suffix="%" />;
              },
            },
            {
              title: '状态',
              width: 90,
              render: (_: unknown, f: Factor) => {
                const st = (f as any).status as string | undefined;
                return st === 'completed' ? (
                  <Tag color="success">已完成</Tag>
                ) : (
                  <Tag color="default">{st || '—'}</Tag>
                );
              },
            },
            {
              title: '操作',
              width: 180,
              fixed: 'right',
              render: (_: unknown, f: Factor) => (
                <span>
                  <Button type="link" size="small" onClick={() => openDetail(f.factorId)}>
                    详情
                  </Button>
                  <Button
                    type="link"
                    size="small"
                    onClick={() => navigate(`/backtest?factorId=${f.factorId}`)}
                  >
                    回测
                  </Button>
                  <Button type="link" size="small" loading={explaining} onClick={() => explain(f.factorId)}>
                    解释
                  </Button>
                </span>
              ),
            },
          ]}
        />
      </div>

      <Drawer
        title="因子详情"
        width={640}
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        extra={
          detail ? (
            <span>
              <Button
                size="small"
                onClick={() => navigate(`/backtest?factorId=${detail.factorId}`)}
              >
                去回测
              </Button>{' '}
              <Button size="small" onClick={() => exportIt(detail.factorId)}>
                导出
              </Button>
            </span>
          ) : null
        }
      >
        {detail && (
          <div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span className="qm-factor-name" style={{ fontSize: 16 }}>
                {detail.factorName}
              </span>
              <QualityBadge quality={detail.quality} />
            </div>
            <div className="qm-factor-id" style={{ margin: '4px 0 12px' }}>
              {detail.factorId}
            </div>
            {groups.map((g) => (
              <div key={g.title} style={{ marginBottom: 14 }}>
                <div style={{ fontWeight: 700, fontSize: 13, marginBottom: 6 }}>{g.title}</div>
                <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 16px' }}>
                  {g.rows.map(([label, v, dec]) => (
                    <div key={label} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
                      <span style={{ color: '#64748b' }}>{label}</span>
                      <ToneNum value={numVal(v)} decimals={dec ?? 4} />
                    </div>
                  ))}
                </div>
              </div>
            ))}
            <p>
              <b>表达式</b>
              <pre
                style={{ background: '#f6f8fb', padding: 10, borderRadius: 8, whiteSpace: 'pre-wrap' }}
              >
                {detail.factorExpression || '—'}
              </pre>
            </p>
            {detail.factorDescription && (
              <p>
                <b>描述</b>
                <br />
                {detail.factorDescription}
              </p>
            )}
            <p style={{ fontSize: 13, color: '#64748b' }}>
              <b>数据溯源</b>
              <br />
              数据集：{(meta.sourceDatasets ?? []).join(', ') || '—'}
              <br />
              取数版本：{meta.qdVersion || '—'}　数据版本：
              {String(meta.dataVersion || '—').slice(0, 24)}
            </p>
          </div>
        )}
      </Drawer>
    </div>
  );
}
