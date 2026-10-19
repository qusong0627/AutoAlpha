import { useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { App, Button, Drawer, Input, Modal, Popconfirm, Select, Table } from 'antd';
import ReactMarkdown from 'react-markdown';
import { Database, Trash2 } from 'lucide-react';
import { ALPHA, dateTime, del, errorText, factorDisplayName, get, number, post, universeName, universeNames, type Factor, type Row } from './api';
import { useResource, useStored } from './hooks';
import { CopyableBlock, JsonDetails, LoadState, MetricGrid, Page, Panel, Refresh, Stat, Status } from './ui';

/** 状态列的两个轴，分开显示（勿再混用同一个标签）：
 *
 *  - **成熟度**（``lifecycle.maturity``，装配层按「有无成功回测 + 盲窗是否到期」
 *    派生）：实验 / 观察 / 在用 / 退役 —— 因子经营的主视角，允许在一个标签里
 *    表达「走到哪一步」；
 *  - **回测处理态**（因子行 ``status``）：只在**异常/进行中**（failed /
 *    backtesting / cancelled）时补一个标签。``pending`` / ``completed`` 不再显示：
 *    它们是旧 CN 回填链的产物，与成熟度并排会出现「同一生命周期两种标签」
 *    （16 个因子同为 observing，却 10 个显示「观察」、6 个显示「已完成」）。
 */
const maturityTag = (row: Row) => {
  const maturity = row.lifecycle?.maturity;
  return maturity && maturity !== 'unknown' ? String(maturity) : row.status;
};
const operationalTag = (row: Row) =>
  ['failed', 'backtesting', 'cancelled'].includes(String(row.status))
    ? String(row.status)
    : undefined;

export default function FactorsPage() {
  const navigate = useNavigate(); const [params, setParams] = useSearchParams(); const { message } = App.useApp();
  const taskId = params.get('taskId') || undefined;
  const [filter, setFilter] = useStored('autoalpha.factor-filter', { search: '', universe: '', direction: '' }); const [page, setPage] = useState(1);
  const [detailId, setDetailId] = useState(''); const [busy, setBusy] = useState(''); const [explanation, setExplanation] = useState('');
  const factors = useResource(signal => get<{ factors: Factor[]; total: number; quality_counts: Row }>(`${ALPHA}/factors`, { limit: 5, offset: (page - 1) * 5, task_id: taskId, universe: filter.universe || undefined }, signal), [page, taskId, filter.universe]);
  const detail = useResource(signal => get<Row>(`${ALPHA}/factors/${encodeURIComponent(detailId)}`, undefined, signal), [detailId], 0, Boolean(detailId));
  const factor = detail.data?.factor || detail.data;
  const visible = factors.data?.factors.filter(f => (!filter.search || `${f.factor_name_cn || ''} ${f.factor_name} ${f.factor_id} ${f.factor_formulation || ''}`.toLowerCase().includes(filter.search.toLowerCase())) && (!filter.direction || (typeof f.ic_value === 'number' && (filter.direction === 'positive' ? f.ic_value >= 0 : f.ic_value < 0)))) || [];
  const deleteFactor = async (row: Factor) => {
    setBusy(`delete:${row.factor_id}`);
    try {
      const result = await del<{ local_files_deleted: boolean }>(`${ALPHA}/factors/${encodeURIComponent(row.factor_id)}`);
      message.success(result.local_files_deleted ? '因子及关联记录、面板文件已删除' : '因子记录已删除，本地文件清理待处理');
      if (detailId === row.factor_id) setDetailId('');
      if (factors.data?.factors.length === 1 && page > 1) setPage(page - 1);
      else factors.refresh();
    } catch (e) { message.error(errorText(e)); }
    finally { setBusy(''); }
  };
  const explain = async () => { setBusy('explain'); try { const result = await post<Row>(`${ALPHA}/factors/${detailId}/explain`); setExplanation(result.explanation || result.text || result.content || '服务没有返回解释文本'); } catch (e) { message.error(errorText(e)); } finally { setBusy(''); } };
  const downloadCode = () => { if (!factor?.factor_code) return; const url = URL.createObjectURL(new Blob([factor.factor_code], { type: 'text/plain;charset=utf-8' })); const a = document.createElement('a'); a.href = url; a.download = `factor-${detailId}.txt`; a.click(); URL.revokeObjectURL(url); };
  return <Page eyebrow={<><Database size={14} />因子资产 · IC/IR · 生命周期</>} title="因子库" description={taskId ? `查看任务 ${taskId} 的产出` : '管理已生成因子，查看表达式、研究指标与生命周期。'} actions={<Refresh onClick={factors.refresh} />}>
    <div className="stat-grid"><Stat label="筛选域因子总数" value={number(factors.data?.total, 0)} /><Stat label="高质量" value={number(factors.data?.quality_counts?.high, 0)} /><Stat label="中等质量" value={number(factors.data?.quality_counts?.medium, 0)} /><Stat label="低质量" value={number(factors.data?.quality_counts?.low, 0)} /></div>
    <Panel title="因子列表"><div className="filter-bar"><Input.Search aria-label="搜索当前页因子" placeholder="搜索当前页名称 / ID / 表达式" allowClear value={filter.search} onChange={e => setFilter({ ...filter, search: e.target.value })} /><Select aria-label="筛选股票池" placeholder="全部股票池" allowClear value={filter.universe || undefined} options={Object.entries(universeNames).map(([value, label]) => ({ value, label }))} onChange={value => { setFilter({ ...filter, universe: value || '' }); setPage(1); }} /><Select aria-label="因子方向" allowClear value={filter.direction || undefined} placeholder="全部方向" options={[{ value: 'positive', label: '正向 IC' }, { value: 'negative', label: '反向 IC' }]} onChange={value => setFilter({ ...filter, direction: value || '' })} />{taskId && <Button onClick={() => { setParams({}); setPage(1); }}>清除任务筛选</Button>}</div>
      <LoadState resource={factors}><Table rowKey="factor_id" dataSource={visible} scroll={{ x: 900 }} pagination={{ current: page, pageSize: 5, total: factors.data?.total, onChange: setPage, showSizeChanger: false }} columns={[
        { title: '因子', width: 280, render: (_, row) => <div><div className="item-title">{factorDisplayName(row)}</div>{row.factor_name_cn && row.factor_name_cn !== row.factor_name && <div className="item-description">原名：{row.factor_name}</div>}<div className="item-id">{row.factor_id.slice(0, 12)}</div><div className="item-description">{row.factor_formulation}</div></div> },
        { title: 'IC', render: (_, row) => number(row.ic_value) }, { title: 'Rank IC', render: (_, row) => number(row.rank_ic) }, { title: 'ICIR', render: (_, row) => number(row.metadata?.icir) },
        { title: '股票池', render: (_, row) => universeName(row.universe) }, { title: '状态', render: (_, row) => <span className="status-cell"><Status value={maturityTag(row)} />{operationalTag(row) && <Status value={operationalTag(row)} />}</span> },
        { title: '操作', width: 250, render: (_, row) => <div className="actions"><Button type="link" onClick={() => setDetailId(row.factor_id)}>详情</Button><Button type="link" onClick={() => navigate(`/backtest?factorId=${row.factor_id}`)}>回测</Button><Popconfirm title="永久删除此因子？" description="将删除因子记录、回测历史、因子池/组合关联及本地面板缓存；已发布到 rd_mined 的共享数据列会保留。" okText="删除" cancelText="取消" okButtonProps={{ danger: true }} onConfirm={() => deleteFactor(row)}><Button type="link" danger loading={busy === `delete:${row.factor_id}`} disabled={row.status === 'backtesting'} icon={<Trash2 size={14} />}>删除</Button></Popconfirm></div> },
      ]} /></LoadState>
      <p className="subtle">查看因子指标，打开详情后可直接发起回测。</p>
    </Panel>
    <Drawer title="因子详情" open={Boolean(detailId)} onClose={() => setDetailId('')} width={Math.min(window.innerWidth - 20, 760)}><LoadState resource={detail}>{factor && <><h2>{factorDisplayName(factor)}</h2>{factor.factor_name_cn && factor.factor_name_cn !== factor.factor_name && <p className="subtle">原始名称：{factor.factor_name}</p>}<p className="subtle">{universeName(factor.universe)} · {factor.date_range || '窗口未记录'}{factor.metrics_source === 'run_ledger' && ' · 指标来自回测台账'}</p><span className="status-cell"><Status value={maturityTag(factor)} />{operationalTag(factor) && <Status value={operationalTag(factor)} />}</span><MetricGrid metrics={{ ...factor.metadata, ...factor }} items={[[ 'ic_value', 'IC' ],[ 'rank_ic', 'Rank IC' ],[ 'icir', 'ICIR' ],[ 'rank_icir', 'Rank ICIR' ]]} /><p>{factor.factor_formulation}</p><CopyableBlock className="code-box" text={factor.factor_code || '暂无实现代码'} /><div className="actions"><Button loading={busy === 'explain'} onClick={explain}>AI 解释</Button><Button disabled={!factor.factor_code} onClick={downloadCode}>下载因子代码</Button><Button type="primary" onClick={() => { setDetailId(''); navigate(`/backtest?factorId=${factor.factor_id}`); }}>独立回测</Button></div><MetricGrid metrics={factor.metadata} items={[[ 'oos_rank_ic', '样本外 Rank IC' ],[ 'pfs', 'PFS' ],[ 'rre', 'RRE' ],[ 'ann_turnover', '年化换手' ]]} /><p className="subtle">创建于 {dateTime(factor.created_at)}</p><JsonDetails data={{ tags: factor.tags, lifecycle: factor.lifecycle, metadata: factor.metadata }} label="查看标签、生命周期与完整研究证据" /></>}</LoadState></Drawer>
    <Modal title="因子解释" open={Boolean(explanation)} onCancel={() => setExplanation('')} footer={<Button onClick={() => setExplanation('')}>关闭</Button>} width={720}><div className="markdown-body"><ReactMarkdown>{explanation}</ReactMarkdown></div></Modal>
  </Page>;
}
