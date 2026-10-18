import { useState } from 'react';
import { Alert, App, Button, Checkbox, Input, Modal, Select, Table, Tag } from 'antd';
import { AlertCircle, CheckCircle2, Clock3, Layers } from 'lucide-react';
import { TRAINING, datasetNames, dateTime, errorText, get, number, post, put, type Row } from './api';
import { useResource, useStored } from './hooks';
import { useWorkspace } from './WorkspaceContext';
import { CopyableBlock, EmptyState, JsonDetails, LoadState, Page, Panel, Refresh, SpecPanel, Status } from './ui';

export default function DataPage() {
  const { message } = App.useApp(); const overview = useWorkspace(); const [source, setSource] = useStored('autoalpha.training-source', 'l1_factors');
  const [versionId, setVersionId] = useState(''); const [versionName, setVersionName] = useState(''); const [busy, setBusy] = useState(''); const [mapping, setMapping] = useState<Row>(); const [fieldsOpen, setFieldsOpen] = useState(false); const [receipt, setReceipt] = useState<Row>(); const [publishReview, setPublishReview] = useState(false);
  const sources = useResource(signal => get<Row>(`${TRAINING}/sources`, { market: 'CN' }, signal));
  const versions = useResource(signal => get<Row>(`${TRAINING}/versions`, { source_dataset: source, market: 'CN' }, signal), [source]);
  const catalog = useResource(signal => get<Row>(`${TRAINING}/catalog`, { source_dataset: source, version_id: versionId || undefined, market: 'CN' }, signal), [source, versionId]);
  const fields = useResource(signal => get<Row>(`${TRAINING}/fields`, { source_dataset: source, market: 'CN' }, signal), [source], 0, fieldsOpen);
  const selectedVersion = versions.data?.versions?.find((v: Row) => v.version_id === versionId);
  const editable = selectedVersion?.status === 'draft';
  const mappings: Row[] = (catalog.data?.categories || []).flatMap((category: Row) => category.features || []);
  const refresh = () => { overview.refresh(); sources.refresh(); versions.refresh(); catalog.refresh(); if (fieldsOpen) fields.refresh(); };
  const act = async (action: string) => {
    setBusy(action);
    try {
      let result: Row;
      if (action === 'materialize') { result = await post<Row>(`${TRAINING}/rd-mined/materialize/start`); message.info(result.started ? '物化进程已启动' : result.message || '已收到物化请求，请查看运行状态'); overview.material.refresh(); }
      else if (action === 'promote') { result = await post<Row>('/factor-research/promote'); message.success('毕业桥请求已提交'); }
      else if (action === 'sources') { result = await post<Row>(`${TRAINING}/sources/refresh`, undefined, { market: 'CN' }); sources.refresh(); fields.refresh(); message.success('字段扫描完成'); }
      else if (action === 'create') { if (!versionName.trim()) throw new Error('请填写草稿版本名称'); result = await post<Row>(`${TRAINING}/versions`, { version_name: versionName.trim(), source_dataset: source }, { market: 'CN' }); setVersionId(result.version_id); setVersionName(''); versions.refresh(); message.success('已创建草稿'); }
      else if (action === 'seed') { result = await post<Row>(`${TRAINING}/versions/${versionId}/seed`); catalog.refresh(); versions.refresh(); message.success('已导入已发现字段'); }
      else if (action === 'publish') { result = await post<Row>(`${TRAINING}/versions/${versionId}/publish`); catalog.refresh(); versions.refresh(); setPublishReview(false); message.success('训练特征目录已发布'); }
      else if (action === 'clone') { result = await post<Row>(`${TRAINING}/versions/${versionId}/clone`, { version_name: `${selectedVersion.version_name} · 副本` }); setVersionId(result.version_id); versions.refresh(); message.success('已克隆为新草稿'); }
      else if (action === 'mapping') { if (!mapping) return; const payload = { ...mapping, source_dataset: source }; for (const key of ['source_column', 'feature_key', 'display_name', 'category_id', 'category_name']) if (!String(payload[key] || '').trim()) throw new Error('请完整填写字段映射'); result = await put<Row>(`${TRAINING}/versions/${versionId}/mappings`, { mapping: payload }); setMapping(undefined); catalog.refresh(); versions.refresh(); message.success('字段映射已保存'); }
      else return;
      setReceipt(result);
    } catch (e) { message.error(errorText(e)); } finally { setBusy(''); }
  };
  const summary = overview.summary.data; const material = overview.material.data; const materialOverview = material?.overview;
  const candidateTotal = Number(materialOverview?.candidates?.total) || 0;
  const pendingCount = Number(materialOverview?.candidates?.pending) || 0;
  const skipped = materialOverview?.candidates?.skipped || {};
  const skippedCount = Object.values(skipped).reduce<number>((total, value: any) => total + (Number(typeof value === 'object' ? value?.count : value) || 0), 0);
  const factorColumns = Number(materialOverview?.library?.factor_columns) || 0;
  const materialReady = Boolean(materialOverview?.library?.ready);
  const materialRunning = Boolean(material?.running);
  const materialStatus = materialRunning
    ? { tone: 'running', title: '正在处理挖掘因子', description: '系统正在执行查重与质量检查，并将符合条件的因子写入训练数据。', icon: <Clock3 size={19} /> }
    : pendingCount > 0
      ? { tone: 'pending', title: `${pendingCount} 个因子等待入库`, description: '挖掘结果已生成，尚未写入训练数据。开始处理后会执行质量检查，并记录未通过原因。', icon: <Clock3 size={19} /> }
      : materialReady
        ? { tone: 'ready', title: '训练数据已就绪', description: `${factorColumns} 个因子字段已写入训练数据分区，可以继续发布训练目录或毕业到研究数据。`, icon: <CheckCircle2 size={19} /> }
        : candidateTotal > 0 && skippedCount > 0
          ? { tone: 'warning', title: '处理完成，但没有可用数据', description: `${skippedCount} 个因子未通过处理；请查看诊断信息中的逐项原因。`, icon: <AlertCircle size={19} /> }
          : { tone: 'empty', title: '还没有可处理的因子', description: '完成因子挖掘后，候选结果会显示在这里。符合条件的因子可以写入训练数据。', icon: <Layers size={19} /> };
  const sourceOptions = Array.from(new Set(['l1_factors', 'l2_factors', 'l1_l2_factors', 'alpha_library', 'rd_mined', ...Object.keys(sources.data?.sources || {})])).map(value => ({ value, label: datasetNames[value] || value }));
  return <Page eyebrow={<><Layers size={14} />训练特征 · 物化 · 字段注册</>} title="数据与特征" description="核对数据覆盖，推进因子物化与训练特征目录发布。" actions={<Refresh onClick={refresh} />}>
    <SpecPanel icon={<Layers size={16} />} title="数据覆盖与物化规格" note={`${summary?.date_range?.start || '—'} → ${summary?.date_range?.end || '—'}`} items={[
      ['证券数量', number(summary?.stock_count, 0), 'A 股可回测标的', '#2563eb'],
      ['交易日', number(summary?.date_range?.trading_days, 0), '当前数据窗口', '#059669'],
      ['待物化', number(materialOverview?.candidates?.pending, 0), '等待写入训练分区', '#f59e0b'],
      ['物化分区', number(materialOverview?.library?.partitions, 0), '已生成数据分区', '#a855f7'],
    ]} />
    <Panel title="当前数据覆盖"><LoadState resource={overview.summary}><p className="subtle">{summary?.date_range?.start || '—'} — {summary?.date_range?.end || '—'}</p><Table rowKey="key" pagination={false} dataSource={Object.entries(summary?.datasets || {}).map(([key, value]) => ({ key, ...value }))} columns={[{ title: '数据集', render: (_, row) => datasetNames[row.key] || row.key }, { title: '字段数', render: (_, row) => number(row.columns, 0) }, { title: '状态', render: () => <Tag color={summary?.available ? 'success' : 'default'}>{summary?.available ? '已接入' : '待准备'}</Tag> }]} /></LoadState></Panel>
    <Panel title="挖掘因子物化" extra={<Button disabled={materialRunning || !materialReady} loading={busy === 'promote'} onClick={() => act('promote')}>毕业到研究数据</Button>}>
      <LoadState resource={overview.material}><div className={`material-status material-status-${materialStatus.tone}`}>
          <div className="material-status-icon">{materialStatus.icon}</div>
          <div className="material-status-copy"><h3>{materialStatus.title}</h3><p>{materialStatus.description}</p>
            <div className="material-status-counts"><span>候选 {number(candidateTotal, 0)}</span><span>待处理 {number(pendingCount, 0)}</span><span>已入库 {number(factorColumns, 0)}</span>{skippedCount > 0 && <span>未通过 {number(skippedCount, 0)}</span>}</div>
          </div>
          {pendingCount > 0 && <Button type="primary" loading={busy === 'materialize'} disabled={!material || materialRunning} onClick={() => act('materialize')}>{materialRunning ? '正在处理' : '处理待物化因子'}</Button>}
        </div>
        {material?.log?.lines?.length > 0 && <details className="json-details"><summary>查看运行日志</summary><CopyableBlock className="log-box" text={material.log.lines.join('\n')} /></details>}
        <JsonDetails data={materialOverview} label="技术诊断信息" />
      </LoadState>
    </Panel>
    <Panel title="训练特征目录" extra={<Select aria-label="训练数据来源" value={source} options={sourceOptions} onChange={value => { setSource(value); setVersionId(''); setMapping(undefined); }} />}>
      {sources.error && <Alert type="warning" showIcon message="数据源状态暂时不可用" description={sources.error} action={<Button onClick={sources.refresh}>重试</Button>} />}
      <div className="filter-bar"><Input aria-label="草稿版本名称" style={{ maxWidth: 300 }} placeholder="填写新草稿名称" value={versionName} onChange={e => setVersionName(e.target.value)} maxLength={128} /><Button loading={busy === 'create'} disabled={!versionName.trim()} onClick={() => act('create')}>创建草稿</Button><Button loading={busy === 'sources'} onClick={() => act('sources')}>扫描数据字段</Button><Button onClick={() => setFieldsOpen(value => !value)}>{fieldsOpen ? '收起字段' : '查看可用字段'}</Button></div>
      <LoadState resource={versions}>{versions.data?.versions?.length ? <Table rowKey="version_id" dataSource={versions.data.versions} pagination={{ pageSize: 8 }} scroll={{ x: 650 }} columns={[{ title: '版本', dataIndex: 'version_name' }, { title: '状态', render: (_, row: Row) => <Status value={row.status} /> }, { title: '映射字段', render: (_, row: Row) => number(row.mapping_count, 0) }, { title: '创建时间', render: (_, row: Row) => dateTime(row.created_at) }, { title: '操作', render: (_, row: Row) => <Button type="link" onClick={() => setVersionId(row.version_id)}>查看目录</Button> }]} /> : <EmptyState title="该来源暂无目录版本" description="扫描数据字段后创建草稿，检查映射，再发布为训练可读目录。" />}</LoadState>
      {fieldsOpen && <LoadState resource={fields}><Table rowKey="column_name" dataSource={fields.data?.fields || []} pagination={{ pageSize: 10 }} columns={[{ title: '字段', dataIndex: 'column_name' }, { title: '类型', dataIndex: 'data_type' }, { title: '已发现', render: (_, row: Row) => row.is_present ? '是' : '否' }]} /></LoadState>}
    </Panel>
    <Panel title={selectedVersion ? `目录 · ${selectedVersion.version_name}` : '当前已发布目录'} extra={versionId && <><Button disabled={!editable} loading={busy === 'seed'} onClick={() => act('seed')}>导入已发现字段</Button><Button disabled={!editable} onClick={() => setMapping({ source_column: '', feature_key: '', display_name: '', category_id: 'custom', category_name: '自定义因子', enabled: true, default_selected: false, required: false, sort_order: 0 })}>新增字段映射</Button><Button disabled={selectedVersion?.status !== 'published'} loading={busy === 'clone'} onClick={() => act('clone')}>克隆为草稿</Button><Button type="primary" disabled={!editable || !mappings.length} onClick={() => setPublishReview(true)}>发布目录</Button></>}>
      <LoadState resource={catalog}>{mappings.length ? <Table rowKey="feature_id" dataSource={mappings} pagination={{ pageSize: 12 }} scroll={{ x: 700 }} columns={[{ title: '特征名', dataIndex: 'feature_name' }, { title: '来源列', dataIndex: 'source_column' }, { title: '特征键', dataIndex: 'key' }, { title: '类别', dataIndex: 'category_name' }, { title: '启用', render: (_, row: Row) => row.enabled ? '是' : '否' }, { title: '操作', render: (_, row: Row) => <Button type="link" disabled={!editable} onClick={() => setMapping({ mapping_id: row.feature_id, source_column: row.source_column, feature_key: row.key, display_name: row.feature_name, category_id: row.category_id, category_name: row.category_name, enabled: row.enabled, default_selected: row.default_selected, required: row.required, sort_order: row.order_no })}>编辑</Button> }]} /> : <EmptyState title={versionId ? '该版本尚无字段映射' : '尚无已发布目录'} description={versionId ? '导入已发现字段或添加映射后，可检查并发布。' : '选择或创建草稿版本，完成字段映射后显式发布。'} />}</LoadState>
    </Panel>
    {receipt && <Panel title="最近操作回执"><JsonDetails data={receipt} label="查看处理结果与跳过明细" /></Panel>}
    <Modal title="编辑字段映射" open={Boolean(mapping)} onCancel={() => setMapping(undefined)} onOk={() => act('mapping')} confirmLoading={busy === 'mapping'} okText="保存映射" cancelText="取消"><div className="form-grid">{[['source_column', '来源列'], ['feature_key', '特征键'], ['display_name', '显示名称'], ['category_id', '类别标识'], ['category_name', '类别名称']].map(([key, label]) => <div className="form-field" key={key}><label htmlFor={`mapping-${key}`}>{label}</label><Input id={`mapping-${key}`} value={mapping?.[key]} onChange={e => setMapping({ ...mapping, [key]: e.target.value })} /></div>)}<div className="form-field"><label>启用字段</label><Checkbox checked={mapping?.enabled} onChange={e => setMapping({ ...mapping, enabled: e.target.checked })}>启用</Checkbox></div></div></Modal>
    <Modal title="发布训练特征目录" open={publishReview} onCancel={() => setPublishReview(false)} onOk={() => act('publish')} confirmLoading={busy === 'publish'} okText="发布此版本" cancelText="返回检查"><p>{selectedVersion?.version_name}</p><p>{datasetNames[source] || source} · {mappings.length} 个映射字段</p><p className="subtle">发布后，训练侧将按此版本读取特征目录。</p></Modal>
  </Page>;
}
