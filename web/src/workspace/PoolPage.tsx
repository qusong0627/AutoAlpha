import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, App, Button, Checkbox, Table, Tag } from 'antd';
import { Network } from 'lucide-react';
import { ALPHA, errorText, factorDisplayName, get, number, post, type Row } from './api';
import { useResource } from './hooks';
import { Chart as ReactECharts, EmptyState, JsonDetails, LoadState, Page, Panel, Refresh, Stat } from './ui';

export default function PoolPage() {
  const navigate = useNavigate(); const { message } = App.useApp(); const [archived, setArchived] = useState(false); const [page, setPage] = useState(1); const [selected, setSelected] = useState<React.Key[]>([]); const [busy, setBusy] = useState(''); const [graphOpen, setGraphOpen] = useState(false); const [suggestionsOpen, setSuggestionsOpen] = useState(false);
  const overview = useResource(signal => get<Row>(`${ALPHA}/pool/overview`, { market: 'a_share' }, signal));
  const factors = useResource(signal => get<Row>(`${ALPHA}/pool/factors`, { market: 'a_share', include_archived: archived, limit: 30, offset: (page - 1) * 30 }, signal), [archived, page]);
  const status = useResource(signal => get<Row>(`${ALPHA}/pool/refresh/status`, undefined, signal), [], 10000);
  const graph = useResource(signal => get<Row>(`${ALPHA}/pool/graph`, { market: 'a_share' }, signal), [], 0, graphOpen);
  const cleanup = useResource(signal => get<Row>(`${ALPHA}/pool/cleanup/suggestions`, { market: 'a_share' }, signal), [], 0, suggestionsOpen);
  const refreshAll = () => { factors.refresh(); overview.refresh(); status.refresh(); if (graphOpen) graph.refresh(); };
  const act = async (action: string) => { setBusy(action); try { const result = action === 'refresh' ? await post<Row>(`${ALPHA}/pool/refresh`, undefined, { market: 'a_share', dry_run: false }) : await post<Row>(`${ALPHA}/pool/cleanup/${action}`, { factor_ids: selected }); message.success(action === 'refresh' ? '已请求刷新因子池' : '处理完成'); if (result.skipped?.length) message.info(`${result.skipped.length} 项未处理，可刷新后核对`); setSelected([]); refreshAll(); } catch (e) { message.error(errorText(e)); } finally { setBusy(''); } };
  return <Page eyebrow={<><Network size={14} />入池评估 · 多样性与谱系</>} title="因子池" description="跟踪因子质量、多样性、检索使用与谱系关系。" actions={<><Refresh onClick={refreshAll} /><Button type="primary" disabled={status.data?.running} loading={busy === 'refresh'} onClick={() => act('refresh')}>刷新因子池</Button></>}>
    <div className="stat-grid"><Stat label="池内因子" value={number(overview.data?.total, 0)} /><Stat label="具备面板" value={number(overview.data?.with_panel, 0)} /><Stat label="平均新颖度" value={number(overview.data?.avg_novelty)} /><Stat label="已归档" value={number(overview.data?.archived_count, 0)} /></div>
    {overview.error && <Alert type="error" message={overview.error} action={<Button onClick={overview.refresh}>重试</Button>} />}
    {status.data?.running && <Alert type="info" showIcon message="因子池正在刷新" description="刷新完成后可重新读取池内评分与谱系。" action={<Button onClick={refreshAll}>读取结果</Button>} />}
    <Panel title="池内资产" extra={<Checkbox checked={archived} onChange={e => { setArchived(e.target.checked); setPage(1); }}>包含已归档因子</Checkbox>}>
      <LoadState resource={factors}>{factors.data?.total ? <Table rowKey="factor_id" dataSource={factors.data.items} rowSelection={{ selectedRowKeys: selected, onChange: setSelected }} scroll={{ x: 850 }} pagination={{ current: page, total: factors.data.total, pageSize: 30, onChange: setPage, showSizeChanger: false }} columns={[
        { title: '因子', render: (_, row: Row) => <div><div className="item-title">{factorDisplayName(row)}</div>{row.factor_name_cn && row.factor_name_cn !== row.factor_name && <div className="item-description">原名：{row.factor_name}</div>}<div className="item-description">{row.category_label}</div>{row.archived_at && <Tag>已归档</Tag>}</div> },
        { title: '池评分', render: (_, row: Row) => number(row.pool_score) }, { title: '新颖度', render: (_, row: Row) => number(row.novelty) }, { title: 'IC', render: (_, row: Row) => number(row.ic_value) }, { title: '被检索次数', render: (_, row: Row) => number(row.times_retrieved, 0) }, { title: '面板', render: (_, row: Row) => row.has_panel ? <Tag color="success">可用</Tag> : <Tag>未准备</Tag> },
      ]} /> : <EmptyState title="因子池暂无资产" description="已有因子需要完成评估并通过入池规则。刷新因子池后，可查看评分、冗余与谱系关系。" action={<Button onClick={() => navigate('/factors')}>查看因子库</Button>} />}</LoadState>
      <div className="selection-summary"><span className="subtle">已选 {selected.length} 项</span><Button disabled={!selected.length} loading={busy === 'archive'} onClick={() => act('archive')}>归档</Button><Button disabled={!selected.length} loading={busy === 'unarchive'} onClick={() => act('unarchive')}>恢复归档</Button><Button disabled={selected.length < 2} onClick={() => navigate(`/combos?factorIds=${selected.join(',')}`)}>组合优化</Button></div>
    </Panel>
    <Panel title="多样性与清理"><div className="actions"><Button onClick={() => setGraphOpen(value => !value)}>{graphOpen ? '收起谱系图' : '查看谱系图'}</Button><Button onClick={() => setSuggestionsOpen(value => !value)}>{suggestionsOpen ? '收起清理建议' : '查看清理建议'}</Button></div>
      {graphOpen && <LoadState resource={graph}>{graph.data?.nodes?.length ? <ReactECharts style={{ height: 400 }} option={{ tooltip: {}, series: [{ type: 'graph', layout: 'force', roam: true, label: { show: true, position: 'right' }, force: { repulsion: 150 }, data: graph.data.nodes.map((node: Row) => ({ ...node, id: node.factor_id || node.id, name: factorDisplayName({ ...node, factor_name: node.name }), symbolSize: 22, itemStyle: { color: '#5c70f4' } })), links: (graph.data.edges || []).map((edge: Row) => ({ source: edge.src_factor_id, target: edge.dst_factor_id, value: edge.relation })) }] }} /> : <EmptyState title="暂无谱系关系" description="池内积累因子后，这里显示实验来源与相关性关系。" />}</LoadState>}
      {suggestionsOpen && <LoadState resource={cleanup}>{cleanup.data?.items?.length ? <Table pagination={false} rowKey="factor_id" dataSource={cleanup.data.items} columns={[{ title: '因子', dataIndex: 'factor_name' }, { title: '清理依据', render: (_, row: Row) => (row.reasons || []).map((reason: Row | string, i: number) => <p key={i}>{typeof reason === 'string' ? reason : reason.message || reason.reason || JSON.stringify(reason)}</p>) }]} /> : <p className="helper">当前没有清理建议。</p>}<JsonDetails data={cleanup.data?.criteria} label="查看清理规则" /></LoadState>}
    </Panel>
  </Page>;
}
