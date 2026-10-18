import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { Alert, App, Button, Input, InputNumber, Select, Table } from 'antd';
import { Combine } from 'lucide-react';
import { ALPHA, dateTime, errorText, factorDisplayName, get, isActive, number, post, type Row } from './api';
import { useResource } from './hooks';
import { CurveChart, EmptyState, JsonDetails, LoadState, MetricGrid, Page, Panel, Refresh, Status } from './ui';

export default function CombosPage() {
  const { message } = App.useApp(); const navigate = useNavigate(); const [params, setParams] = useSearchParams();
  const [selected, setSelected] = useState<string[]>(params.get('factorIds')?.split(',').filter(Boolean) || []); const [name, setName] = useState(''); const [seed, setSeed] = useState<number>(42); const [busy, setBusy] = useState(false);
  const pool = useResource(signal => get<Row>(`${ALPHA}/pool/factors`, { market: 'a_share', limit: 500 }, signal));
  const combos = useResource(signal => get<Row>(`${ALPHA}/combos`, { limit: 100 }, signal), [], 10000);
  const id = params.get('comboId') || '';
  const detail = useResource(signal => get<Row>(`${ALPHA}/combos/${encodeURIComponent(id)}`, undefined, signal), [id], 5000, Boolean(id));
  const combo = detail.data?.combo || detail.data;
  const available = (pool.data?.items || []).filter((item: Row) => item.has_panel && !item.archived_at);
  const unavailable = selected.filter(value => !available.some((item: Row) => item.factor_id === value));
  const optimize = async () => { setBusy(true); try { const result = await post<Row>(`${ALPHA}/combos/optimize`, { factor_ids: selected, name: name.trim(), seed, market: 'a_share' }); setParams({ comboId: result.combo_id }); combos.refresh(); message.success('组合优化任务已提交'); } catch (e) { message.error(errorText(e)); } finally { setBusy(false); } };
  return <Page eyebrow={<><Combine size={14} />组合优化 · 训练与验证区间</>} title="组合实验" description="基于具备面板数据的因子优化权重，比较训练与验证区间表现。" actions={<Refresh onClick={() => { pool.refresh(); combos.refresh(); if (id) detail.refresh(); }} />}>
    <Panel title="创建组合实验"><LoadState resource={pool}>
      {!available.length && <Alert type="info" showIcon message="当前没有具备面板的池内因子" description="请先完成因子评估与入池，再选择至少两个可用因子。" action={<Button onClick={() => navigate('/pool')}>查看因子池</Button>} />}
      <div className="form-grid"><div className="form-field"><label htmlFor="combo-name">组合名称</label><Input id="combo-name" value={name} onChange={e => setName(e.target.value)} placeholder="例如：量价稳健组合" /></div><div className="form-field"><label htmlFor="combo-seed">随机种子</label><InputNumber id="combo-seed" value={seed} onChange={value => setSeed(value ?? 42)} /></div><div className="form-field wide"><label htmlFor="combo-factors">参与优化的因子</label><Select id="combo-factors" mode="multiple" value={selected} onChange={setSelected} optionFilterProp="label" options={available.map((item: Row) => ({ value: item.factor_id, label: factorDisplayName(item) }))} placeholder="选择至少两个具备面板的因子" /></div></div>
      {unavailable.length > 0 && <p className="helper">已选的 {unavailable.length} 个因子尚无可用面板，请重新选择。</p>}
      <div className="actions"><Button type="primary" loading={busy} disabled={selected.length < 2 || Boolean(unavailable.length)} onClick={optimize}>开始优化权重</Button></div>
    </LoadState></Panel>
    <Panel title="组合记录"><LoadState resource={combos}>{combos.data?.items?.length ? <Table rowKey="combo_id" dataSource={combos.data.items} scroll={{ x: 600 }} pagination={{ pageSize: 10 }} columns={[{ title: '组合', render: (_, row: Row) => <div><div className="item-title">{row.name || row.combo_id}</div><div className="item-id">{row.combo_id}</div></div> }, { title: '状态', render: (_, row: Row) => <Status value={row.status} /> }, { title: '因子数', render: (_, row: Row) => number(row.factor_ids?.length ?? row.n_factors, 0) }, { title: '创建时间', render: (_, row: Row) => dateTime(row.created_at) }, { title: '操作', render: (_, row: Row) => <Button type="link" onClick={() => setParams({ comboId: row.combo_id })}>查看结果</Button> }]} /> : <EmptyState title="还没有组合实验" description="优化完成后，权重、两段区间指标与曲线将保存在这里。" />}</LoadState></Panel>
    {id && <Panel title="组合详情"><LoadState resource={detail}>{combo && <><h3>{combo.name || id}</h3><Status value={combo.status} />{combo.error && <Alert type="error" message={combo.error} />}<p className="subtle">{combo.train_window}</p><div className="two-columns"><div><h3>训练区间</h3><MetricGrid metrics={combo.train_metrics} items={[[ 'mean_rank_ic', 'Rank IC' ],[ 'rank_icir', 'Rank ICIR' ],[ 'sharpe_net', '扣费夏普' ],[ 'ann_return_net', '扣费年化', 'percent' ]]} /></div><div><h3>验证区间</h3><MetricGrid metrics={combo.valid_metrics} items={[[ 'mean_rank_ic', 'Rank IC' ],[ 'rank_icir', 'Rank ICIR' ],[ 'sharpe_net', '扣费夏普' ],[ 'ann_return_net', '扣费年化', 'percent' ]]} /></div></div><Table rowKey="factor_id" pagination={false} dataSource={Object.entries(combo.weights || {}).map(([factor_id, weight]) => ({ factor_id, weight }))} columns={[{ title: '因子', render: (_, row) => factorDisplayName(pool.data?.items?.find((f: Row) => f.factor_id === row.factor_id) || { factor_id: row.factor_id }) }, { title: '组合权重', render: (_, row) => number(row.weight) }]} />{combo.valid_metrics?.curve && <><h3>验证区间扣费净值</h3><CurveChart series={{ dates: combo.valid_metrics.curve.dates, nav_net: combo.valid_metrics.curve.values }} /></>}<JsonDetails data={combo} label="查看完整组合结果" /></>}</LoadState></Panel>}
  </Page>;
}
