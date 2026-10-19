import { useEffect, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { Alert, App, Button, Input, InputNumber, Modal, Segmented, Select, Table, Tag } from 'antd';
import { Activity, BarChart3 } from 'lucide-react';
import { ALPHA, dateTime, errorText, factorDisplayName, get, isActive, number, post, request, universeName, universeNames, type Factor, type Row, type Run } from './api';
import { useResource, useStored } from './hooks';
import { CurveChart, EmptyState, JsonDetails, LoadState, MetricGrid, Page, Panel, Refresh, SpecPanel, Status } from './ui';

/** 已用时文案：<1 分钟按秒，其余 m 分 s 秒（进度容器每秒刷新）。 */
function elapsedText(from: string | undefined, now: number) {
  if (!from) return '—';
  const started = new Date(from).getTime();
  if (!Number.isFinite(started)) return '—';
  const seconds = Math.max(0, Math.floor((now - started) / 1000));
  if (seconds < 60) return `${seconds} 秒`;
  return `${Math.floor(seconds / 60)} 分 ${String(seconds % 60).padStart(2, '0')} 秒`;
}

/** 单笔回测的常规用时（实测 34~40s）；超过它的仍在跑，给一句「还在跑」的提示。 */
const SLOW_RUN_SECONDS = 120;

/** 回测进度容器：口径与台账之间的实时视图（单跑=不定态进度条+已用时，批次=真实完成比例）。 */
function BacktestProgress({ runs, batch, batchId, now, busy, nameOf, onCancel, onCancelBatch }: {
  runs: Run[]; batch?: Row; batchId: string; now: number; busy: string;
  nameOf: (factorId: string, fallback?: string) => string;
  onCancel: () => void; onCancelBatch: () => void;
}) {
  const active = runs.filter(run => isActive(run.status));
  const progress = (batch?.progress || {}) as Row;
  // 只看「还没收口」的批次：已完成的批次留在下方「批量任务」里，不长期占着进度位
  const batchActive = Boolean(batchId && progress.total && isActive(batch?.batch?.status));
  const total = Number(progress.total || 0);
  const done = Number(progress.done || 0);
  const percent = total ? Math.round((done / total) * 100) : 0;
  const current = (progress.current || []) as Row[];
  const idle = !active.length && !batchActive;
  return <Panel title="回测进度" extra={idle ? undefined : <span className="progress-live"><i />实时刷新 · 5s</span>}>
    {batchActive && <div className="progress-board" style={{ marginBottom: active.length ? 10 : 0 }}>
      <article className="progress-row batch">
        <div className="progress-meter">
          <div className="progress-top"><div><strong>批量回测 · {total} 个单元</strong><div className="subtle">批次 {batchId} · {progress.draining ? '串行排水进行中' : '已停止排水'}</div></div><div className="actions"><Status value={batch?.batch?.status} /><Button size="small" danger loading={busy === 'cancel'} onClick={onCancelBatch}>取消该批次</Button></div></div>
          <div className="progress-bar" role="progressbar" aria-label="批量回测完成比例" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}><span style={{ width: `${percent}%` }} /></div>
          <div className="progress-stats">
            <span><b>{done}</b>/{total} 已完成</span><span><b>{progress.running || 0}</b> 运行中</span><span><b>{progress.failed || 0}</b> 失败</span><span><b>{progress.pending || 0}</b> 待跑</span>
            <span className="progress-pct">{percent}%</span>
          </div>
          {current.length > 0 && <div className="progress-current">{current.map(unit => <span className="progress-chip" key={`${unit.factor_id}-${unit.market}`}>{nameOf(String(unit.factor_id))} · {universeName(unit.market)}</span>)}</div>}
        </div>
      </article>
    </div>}
    {active.length > 0 && <div className="progress-board">{active.map(run => <article className="progress-row" key={run.run_id}>
      <div className="progress-main">
        <strong>{nameOf(run.factor_id, run.factor_name)}</strong>
        <span className="subtle">{run.date_range || '窗口待后端确认'} · {universeName(run.universe)} · 日频</span>
      </div>
      <div className="progress-meter">
        <div className="progress-bar indeterminate" role="progressbar" aria-label="单因子回测进行中" aria-valuetext="进行中（无百分比）"><span /></div>
        <span className="progress-meta">
          <Status value={run.status} />
          <span>已用时 {elapsedText(run.created_at, now)}</span>
          {run.created_at && (now - new Date(run.created_at).getTime()) / 1000 > SLOW_RUN_SECONDS && <span className="progress-note">已超过常规用时，仍在运行</span>}
        </span>
      </div>
      <div className="actions"><Button size="small" danger loading={busy === 'cancel'} onClick={onCancel}>取消</Button></div>
    </article>)}</div>}
    {idle && <div className="progress-idle"><span className="progress-idle-icon"><Activity size={16} /></span><strong>当前没有进行中的回测</strong><div className="subtle">提交后这里会实时显示状态与已用时，收口结果随后进入下方台账。</div></div>}
  </Panel>;
}

export default function BacktestPage() {
  const { message } = App.useApp(); const [params, setParams] = useSearchParams();
  const [factorId, setFactorId] = useState(params.get('factorId') || ''); const [factorIds, setFactorIds] = useState(params.get('factorIds')?.split(',').filter(Boolean) || []);
  const [mode, setMode] = useState(params.get('factorIds') ? 'batch' : 'single');
  const [config, setConfig] = useStored('autoalpha.backtest-config', { universe: 'csi500', start: '', end: '', cost_bps: 20 });
  const [busy, setBusy] = useState(''); const [review, setReview] = useState<Row>(); const [batchId, setBatchId] = useState(params.get('batchId') || ''); const [receipt, setReceipt] = useState<Row>();
  const factors = useResource(signal => get<{ factors: Factor[] }>(`${ALPHA}/factors`, { limit: 500 }, signal));
  useEffect(() => { if (!factorId && factors.data?.factors.length) setFactorId(factors.data.factors[0].factor_id); }, [factors.data]);
  useEffect(() => { const id = params.get('factorId'); const ids = params.get('factorIds'); if (id) { setFactorId(id); if (!ids) setMode('single'); } if (ids) { setFactorIds(ids.split(',').filter(Boolean)); setMode('batch'); } }, [params]);
  const runs = useResource(signal => get<{ runs: Run[] }>(`/factor-backtest/runs`, { factor_id: factorId, limit: 100 }, signal), [factorId], 5000, Boolean(factorId));
  const batches = useResource(signal => get<Row>('/factor-backtest/batches', { limit: 20 }, signal), [], 15000);
  const batch = useResource(signal => get<Row>('/factor-backtest/batch/status', { batch_id: batchId }, signal), [batchId], 5000, Boolean(batchId));
  const activeRuns = (runs.data?.runs || []).filter(run => isActive(run.status));
  const batchActive = Boolean(batch.data?.progress?.total && isActive(batch.data?.batch?.status));
  // 进度容器里的「已用时」每秒走一格；空闲时不起定时器（不做无用的重渲染）。
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!activeRuns.length && !batchActive) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [activeRuns.length, batchActive]);
  const nameOf = (id: string, fallback?: string) => { const factor = factors.data?.factors.find(f => f.factor_id === id); return factor ? factorDisplayName(factor) : fallback || id; };
  const selectedRun = runs.data?.runs.find(run => run.run_id === params.get('runId')) || runs.data?.runs[0];
  const selectedId = selectedRun?.run_id;
  const series = useResource(signal => get<Row>(`/factor-backtest/runs/${selectedId}/series`, undefined, signal), [selectedId], 0, Boolean(selectedId && selectedRun?.has_series));
  const report = useResource(signal => get<Row>(`/factor-backtest/report/${selectedId}`, undefined, signal), [selectedId], 0, Boolean(selectedId && selectedRun?.status === 'completed'));
  const preview = () => {
    if (mode === 'single' ? !factorId : !factorIds.length) { message.warning('请先选择因子'); return; }
    if (Boolean(config.start) !== Boolean(config.end) || (config.start && config.start > config.end)) { message.warning('请填写完整且有效的回测区间'); return; }
    const payload: Row = { ...(mode === 'single' ? { factor_id: factorId, market: 'a_share', universe: config.universe } : { factor_ids: factorIds, markets: ['a_share'] }), cost_bps: config.cost_bps };
    if (config.start) Object.assign(payload, { start: config.start, end: config.end });
    setReview(payload);
  };
  const submit = async () => {
    if (!review) return; setBusy('submit');
    try { const result = await post<Row>(`/factor-backtest/${mode}`, review); setReceipt(result); if (result.batch_id) { setBatchId(result.batch_id); setParams({ batchId: result.batch_id }); } else { setParams({ factorId, ...(result.run_id ? { runId: result.run_id } : {}) }); } setReview(undefined); runs.refresh(); batches.refresh(); message.success('回测请求已处理，请查看台账与跳过明细'); } catch (e) { message.error(errorText(e)); } finally { setBusy(''); }
  };
  const cancel = async (batchMode = false) => { setBusy('cancel'); try { await post(batchMode ? '/factor-backtest/batch/cancel' : `/factor-backtest/single/${factorId}/cancel`, batchMode ? { batch_id: batchId } : undefined); message.success('已请求取消'); runs.refresh(); batch.refresh(); batches.refresh(); } catch (e) { message.error(errorText(e)); } finally { setBusy(''); } };
  const exportPdf = async () => { if (!selectedId) return; setBusy('pdf'); try { const blob = await request<Blob>(`/factor-backtest/report/${selectedId}/pdf`, { responseType: 'blob', timeout: 120000 }); const url = URL.createObjectURL(blob); const a = document.createElement('a'); a.href = url; a.download = `factor-report-${selectedId}.pdf`; a.click(); URL.revokeObjectURL(url); } catch (e) { message.error(errorText(e)); } finally { setBusy(''); } };
  return <Page eyebrow={<><BarChart3 size={14} />样本外验证 · 全周期回测</>} title="回测中心" description="独立验证因子表现，比较收益、显著性与交易成本。" actions={<Refresh onClick={() => { factors.refresh(); runs.refresh(); batches.refresh(); }} />}>
    <Panel title="创建回测"><LoadState resource={factors}>
      <div className="actions" style={{ marginBottom: 20 }}><Segmented aria-label="回测方式" value={mode} onChange={value => setMode(String(value))} options={[{ value: 'single', label: '单因子' }, { value: 'batch', label: '批量因子' }]} /><Tag color="blue">A 股 · 日频</Tag></div>
      <div className="form-grid"><div className="form-field wide"><label htmlFor="backtest-factor">参与回测的因子</label><Select id="backtest-factor" showSearch optionFilterProp="label" mode={mode === 'batch' ? 'multiple' : undefined} value={mode === 'batch' ? factorIds : factorId || undefined} onChange={value => mode === 'batch' ? setFactorIds(value as string[]) : setFactorId(String(value))} options={factors.data?.factors.map(f => ({ value: f.factor_id, label: factorDisplayName(f), disabled: !f.factor_code }))} placeholder="选择已具备代码的因子" /></div>
        <div className="form-field"><label htmlFor="backtest-universe">股票池</label><Select id="backtest-universe" disabled={mode === 'batch'} value={config.universe} onChange={universe => setConfig({ ...config, universe })} options={Object.entries(universeNames).map(([value, label]) => ({ value, label }))} /></div><div className="form-field"><label htmlFor="backtest-cost">交易成本（bps）</label><InputNumber id="backtest-cost" min={0} max={1000} value={config.cost_bps} onChange={value => setConfig({ ...config, cost_bps: value ?? 0 })} /></div>
        <div className="form-field"><label htmlFor="backtest-start">开始日期（可留空）</label><Input id="backtest-start" type="date" value={config.start} onChange={e => setConfig({ ...config, start: e.target.value })} /></div><div className="form-field"><label htmlFor="backtest-end">结束日期（可留空）</label><Input id="backtest-end" type="date" min={config.start} value={config.end} onChange={e => setConfig({ ...config, end: e.target.value })} /></div>
      </div><p className="subtle">留空日期由后端选择可用窗口；批量回测使用市场默认股票池。其他市场的数据独立性尚未验证。</p>
      <div className="actions"><Button type="primary" onClick={preview}>预览回测任务</Button>{runs.data?.runs.some(run => isActive(run.status)) && <Button danger loading={busy === 'cancel'} onClick={() => cancel()}>取消当前因子回测</Button>}</div>
      {receipt && <JsonDetails data={receipt} label="查看派发回执与跳过明细" />}
    </LoadState></Panel>
    <SpecPanel icon={<BarChart3 size={16} />} title="回测口径" note={mode === 'batch' ? '批量因子 · A 股' : '单因子 · A 股'} items={[
      ['股票池', mode === 'batch' ? '市场默认' : universeName(config.universe), '本次回测范围', '#2563eb'],
      ['窗口', `${config.start || '默认开始'} → ${config.end || '数据末端'}`, '留空由后端选择可用窗口', '#059669'],
      ['交易成本', `${config.cost_bps} bps`, '换手成本口径', '#f59e0b'],
      ['频率', '日频', 'A 股日频数据', '#a855f7'],
    ]} />
    <BacktestProgress runs={runs.data?.runs || []} batch={batch.data} batchId={batchId} now={now} busy={busy} nameOf={nameOf} onCancel={() => cancel()} onCancelBatch={() => cancel(true)} />
    <Panel title="回测台账"><LoadState resource={runs}>{runs.data?.runs.length ? <Table rowKey="run_id" dataSource={runs.data.runs} scroll={{ x: 900 }} pagination={{ pageSize: 8, showSizeChanger: false }} columns={[
      { title: '窗口 / 股票池', render: (_, run) => <div><div>{run.date_range || '等待执行'}</div><div className="item-description">{universeName(run.universe)}</div></div> }, { title: '状态', render: (_, run) => <Status value={run.status} /> }, { title: 'IC', render: (_, run) => number(run.ic_value) }, { title: 'Rank IC', render: (_, run) => number(run.rank_ic) }, { title: '夏普', render: (_, run) => number(run.sharpe_ratio) }, { title: '扣费夏普', render: (_, run) => number(run.metrics?.sharpe_net) }, { title: '创建时间', render: (_, run) => dateTime(run.created_at) }, { title: '操作', render: (_, run) => <Button type="link" onClick={() => setParams({ factorId, runId: run.run_id })}>查看报告</Button> },
    ]} /> : <EmptyState title="该因子尚无回测记录" description="提交独立回测后，运行台账与结果将在这里显示。" />}</LoadState></Panel>
    {selectedRun && <Panel title={`${factorDisplayName(factors.data?.factors.find(f => f.factor_id === selectedRun.factor_id) || selectedRun)} · 验证结果`} extra={<><Status value={selectedRun.status} /><Button disabled={selectedRun.status !== 'completed'} loading={busy === 'pdf'} onClick={exportPdf}>导出 PDF 报告</Button></>}>
      {selectedRun.error && <Alert type="warning" showIcon message={selectedRun.error} />}
      <MetricGrid metrics={{ ...selectedRun.metrics, ...selectedRun }} items={[[ 'ic_value', 'IC' ],[ 'rank_ic', 'Rank IC' ],[ 'icir', 'ICIR' ],[ 'sharpe_ratio', '夏普' ],[ 'annual_return', '年化收益', 'percent' ],[ 'max_drawdown', '最大回撤', 'percent' ]]} />
      <MetricGrid metrics={selectedRun.metrics} items={[[ 'sharpe_net', '扣费夏普' ],[ 'ann_return_net', '扣费年化', 'percent' ],[ 'ann_turnover', '年化换手' ],[ 'cost_bps', '成本（bps）' ]]} />
      <p className="subtle">{selectedRun.date_range} · {universeName(selectedRun.universe)} · 以本次运行的窗口与成本口径为准</p>
      {selectedRun.has_series && <LoadState resource={series}><div className="two-columns"><div><h3>收益曲线 · 基准 / 因子 / 超额</h3><CurveChart series={series.data?.series} kind="returns" title="累计收益 · 基准 / 因子 / 超额" /></div><div><h3>IC 序列 · 红=正 / 绿=负</h3><CurveChart series={series.data?.series} kind="ic" title="IC 序列 · 红=正 / 绿=负" /></div><div><h3>分位组合</h3><CurveChart series={series.data?.series} kind="quantile" title="分位净值" /></div><div><h3>换手率</h3><CurveChart series={series.data?.series} kind="turnover" title="换手率" /></div></div></LoadState>}
      <LoadState resource={report}>{report.data && <JsonDetails data={report.data.report || report.data} label="查看显著性、成本敏感性与机构报告" />}</LoadState>
    </Panel>}
    <Panel title="批量任务"><LoadState resource={batches}><Table rowKey="batch_id" dataSource={batches.data?.batches || batches.data?.items || []} pagination={{ pageSize: 5 }} columns={[{ title: '批次', dataIndex: 'batch_id' }, { title: '状态', render: (_, row: Row) => <Status value={row.status} /> }, { title: '操作', render: (_, row: Row) => <Button type="link" onClick={() => setBatchId(row.batch_id)}>查看进度</Button> }]} /></LoadState>{batchId && <LoadState resource={batch}><JsonDetails data={batch.data} label="查看批次进度与失败单元" /><div className="actions"><Button danger loading={busy === 'cancel'} onClick={() => cancel(true)}>取消该批次</Button></div></LoadState>}</Panel>
    <Modal title="核对回测任务" open={Boolean(review)} onCancel={() => !busy && setReview(undefined)} onOk={submit} confirmLoading={busy === 'submit'} okText="开始回测" cancelText="返回修改"><p>{mode === 'batch' ? `${factorIds.length} 个因子 × A 股市场` : (() => { const factor = factors.data?.factors.find(f => f.factor_id === factorId); return factor ? factorDisplayName(factor) : ''; })()}</p><p>{config.start || '默认开始日期'} — {config.end || '数据可用末端'}</p><p>交易成本 {config.cost_bps} bps</p></Modal>
  </Page>;
}
