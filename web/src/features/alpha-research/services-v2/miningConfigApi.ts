/**
 * 挖掘配置目录 / 分段预览 / 选择 → wire 字段（移植 P1）。
 *
 * 后端唯一契约面 = `GET /alpha-agent/mining/config-options`（可选四端点做预览）。
 * 「预览 == 实跑」的保证来自两侧共用同一组纯函数：这里**只做映射**，
 * 不发明任何默认值、不做本地退让——日期上下限、末端保护、分段全部以
 * 服务端返回为准，前端本地规则一多就会和实跑口径分叉。
 */

import { apiClient } from '../../../services/aiStrategyClients';
import type { ApiResponse, MiningConfigSelection } from '../types-v2';
import { extractDetail } from './docMiningApi';

// ========================== 类型 ==========================

export interface MiningWindowSpan {
  start: string;
  end: string;
}

export interface MiningDatasetOption {
  /** 登记序 key（提交时下发；后端口径见 mining_datasets.SUPPORTED_DATASETS） */
  key: string;
  label: string;
}

/** 频率选项（服务端登记序；value 即提交时下发的 freq） */
export interface MiningFreqOption {
  value: string;
  label: string;
}

/**
 * 分钟数据面事实（仅 freq=1min 时后端返回）。
 *
 * `coverage` 是 provider 全区间（含断层之前的碎片段），`usable` 才是真正
 * 可挖的**末端连续块**——日期选择器按 usable 限位；`gaps` 是自然日断层表，
 * 如实上屏（跨断层窗口会被服务端 400，前端不装作数据是连续的）。
 */
export interface MiningMinuteInfo {
  /** 数据面是否已构建；false 时其余字段无意义，`error` 给原因 */
  ready: boolean;
  providerDir: string | null;
  error: string | null;
  coverage: MiningWindowSpan | null;
  usable: MiningWindowSpan | null;
  usableDays: number;
  gaps: MiningWindowSpan[];
}

export interface MiningConfigOptions {
  datasets: MiningDatasetOption[];
  /** 可选频率目录（day 恒在；1min 视市场）。后端没给 = 不渲染频率选择 */
  freqs: MiningFreqOption[];
  /** 本次预览生效的频率（服务端归一后的值，如 'day'） */
  freq: string;
  /** 分钟数据面信息（非分钟口径 = null） */
  minute: MiningMinuteInfo | null;
  /** QuantDB 日线实际覆盖区间（null = 数据源不可用，此时不设日期限位） */
  coverage: MiningWindowSpan | null;
  /**
   * 回测可用末端（数据末端前一个交易日）：日期选择器上限定它。
   * 晚于该日的窗口终点会被服务端原地退让（qlib 回测按 t+1 成交，
   * 终点须留出次日行情），不是「拒绝」而是「退让」。
   */
  backtestEndGuard: string | null;
  /** 默认窗口（锚定覆盖末端 4 年挖掘 + 1 年检验） */
  defaults: {
    mining: MiningWindowSpan | null;
    validation: MiningWindowSpan | null;
  };
  /** 生效窗口：未给预览参数 = 默认窗口；validation 为 null = 不启用样本外检验 */
  windows: {
    mining: MiningWindowSpan | null;
    validation: MiningWindowSpan | null;
    isDefault: boolean;
  };
  /** qlib 实验分段（train/valid/test），与实跑同源 */
  segments: Partial<Record<'train' | 'valid' | 'test', MiningWindowSpan>>;
  /** 自定义配置适用的市场（当前仅 a_share） */
  scopes: string[];
}

/** 未自定义（提交时不下发任何参数；缓存/任务行与迁移前逐位一致） */
export const EMPTY_MINING_SELECTION: MiningConfigSelection = {
  miningStart: null,
  miningEnd: null,
  validStart: null,
  validEnd: null,
  datasets: null,
  freq: null,
};

// ========================== 选择 → wire ==========================

export interface MiningConfigWire {
  mining_start?: string;
  mining_end?: string;
  valid_start?: string;
  valid_end?: string;
  datasets?: string[];
  freq?: string;
}

/** 频率是否偏离日线默认（'day'/''/null 都是日线默认语义，不产生参数） */
export function isNonDefaultFreq(freq: string | null | undefined): boolean {
  const f = (freq ?? '').trim();
  return Boolean(f && f !== 'day');
}

/**
 * 选择 → 后端字段（snake_case）。两对日期**成对**下发：半截的一对整对
 * 丢弃吗？——不：「成对」由载体（面板输入框恒成对）保证，这里对半截
 * 输入显式抛错，避免静默丢掉用户选的日期。数据集空选/全选一律不下发
 * （None = 全选，任务行零参数痕迹）；频率同理——日线是默认语义，不下发，
 * 显式 'day' 也归一为不发（否则老路径凭空多一个键）。
 */
export function miningConfigWire(
  sel?: MiningConfigSelection | null,
): MiningConfigWire {
  const wire: MiningConfigWire = {};
  if (!sel) return wire;
  if (isNonDefaultFreq(sel.freq)) wire.freq = (sel.freq ?? '').trim();
  const pair = (
    start: string | null | undefined,
    end: string | null | undefined,
    label: string,
  ): [string, string] | null => {
    const s = (start ?? '').trim();
    const e = (end ?? '').trim();
    if (!s && !e) return null;
    if (!s || !e) {
      throw new Error(`${label}须成对（起点与终点都填，或都留空）`);
    }
    return [s, e];
  };
  const mining = pair(sel.miningStart, sel.miningEnd, '挖掘区间');
  if (mining) {
    wire.mining_start = mining[0];
    wire.mining_end = mining[1];
  }
  const validation = pair(sel.validStart, sel.validEnd, '检验区间');
  if (validation) {
    wire.valid_start = validation[0];
    wire.valid_end = validation[1];
  }
  const datasets = (sel.datasets ?? []).map((d) => d.trim()).filter(Boolean);
  if (datasets.length) wire.datasets = datasets;
  return wire;
}

/**
 * 是否携带了任何自定义（未自定义 = 提交走默认路径，零参数）。
 * 只查字段在场，不走 `miningConfigWire`（那会对半截日期抛错——判定用的
 * 谓词不该抛；半截配置是提交时该报的错，由 wire 抛给提交路径兜住）。
 */
export function hasCustomMiningConfig(
  sel?: MiningConfigSelection | null,
): boolean {
  if (!sel) return false;
  return Boolean(
    sel.miningStart ||
      sel.miningEnd ||
      sel.validStart ||
      sel.validEnd ||
      (sel.datasets && sel.datasets.length > 0) ||
      isNonDefaultFreq(sel.freq),
  );
}

/** 追加到已有 query（数据集可重复；未自定义不产生任何键） */
export function appendMiningConfigQuery(
  qs: URLSearchParams,
  sel?: MiningConfigSelection | null,
): URLSearchParams {
  const wire = miningConfigWire(sel);
  if (wire.freq) qs.set('freq', wire.freq);
  for (const key of ['mining_start', 'mining_end', 'valid_start', 'valid_end'] as const) {
    const value = wire[key];
    if (value) qs.set(key, value);
  }
  for (const d of wire.datasets ?? []) qs.append('datasets', d);
  return qs;
}

// ========================== 端点 ==========================

function span(raw: unknown): MiningWindowSpan | null {
  const r = raw as { start?: unknown; end?: unknown } | null | undefined;
  if (!r || typeof r.start !== 'string' || typeof r.end !== 'string') return null;
  return { start: r.start, end: r.end };
}

/** 分钟数据面块（snake→camel）；类型不符一律退化为 null / 空，不伪造事实 */
function minuteInfo(raw: unknown): MiningMinuteInfo | null {
  const r = raw as Record<string, unknown> | null | undefined;
  if (!r || typeof r !== 'object') return null;
  return {
    ready: r.ready === true,
    providerDir: typeof r.provider_dir === 'string' ? r.provider_dir : null,
    error: typeof r.error === 'string' && r.error ? r.error : null,
    coverage: span(r.coverage),
    usable: span(r.usable),
    usableDays: typeof r.usable_days === 'number' ? r.usable_days : 0,
    gaps: Array.isArray(r.gaps)
      ? r.gaps
          .map((g) => span(g))
          .filter((g): g is MiningWindowSpan => g !== null)
      : [],
  };
}

/**
 * 配置目录 + 分段预览。给了 `preview` 即按该组合预览（校验与提交同一口径，
 * 非法组合后端 400，detail 原文上屏）；没给或全空 = 预览默认窗口。
 */
export async function getMiningConfigOptions(
  preview?: MiningConfigSelection | null,
): Promise<ApiResponse<MiningConfigOptions>> {
  try {
    const qs = appendMiningConfigQuery(new URLSearchParams(), preview);
    const suffix = qs.toString();
    const res = await apiClient.get(
      `/alpha-agent/mining/config-options${suffix ? `?${suffix}` : ''}`,
    );
    const raw = res.data?.data ?? {};
    const windows = raw.windows ?? {};
    const defaults = raw.defaults ?? {};
    return {
      success: true,
      data: {
        datasets: Array.isArray(raw.datasets)
          ? raw.datasets
              .filter(
                (d: { key?: unknown }) => typeof d?.key === 'string' && d.key,
              )
              .map((d: { key: string; label?: unknown }) => ({
                key: d.key,
                label: typeof d.label === 'string' && d.label ? d.label : d.key,
              }))
          : [],
        freqs: Array.isArray(raw.freqs)
          ? raw.freqs
              .filter(
                (f: { value?: unknown }) =>
                  typeof f?.value === 'string' && f.value,
              )
              .map((f: { value: string; label?: unknown }) => ({
                value: f.value,
                label:
                  typeof f.label === 'string' && f.label ? f.label : f.value,
              }))
          : [],
        freq: typeof raw.freq === 'string' ? raw.freq : '',
        minute: minuteInfo(raw.minute),
        coverage: span(raw.coverage),
        backtestEndGuard:
          typeof raw.backtest_end_guard === 'string'
            ? raw.backtest_end_guard
            : null,
        defaults: {
          mining: span(defaults.mining),
          validation: span(defaults.validation),
        },
        windows: {
          mining: span(windows.mining),
          validation: span(windows.validation),
          isDefault: windows.is_default !== false,
        },
        segments: {
          train: span(raw.segments?.train) ?? undefined,
          valid: span(raw.segments?.valid) ?? undefined,
          test: span(raw.segments?.test) ?? undefined,
        },
        scopes: Array.isArray(raw.scopes)
          ? raw.scopes.filter((s: unknown): s is string => typeof s === 'string')
          : [],
      },
    };
  } catch (error: unknown) {
    return { success: false, error: extractDetail(error) };
  }
}
