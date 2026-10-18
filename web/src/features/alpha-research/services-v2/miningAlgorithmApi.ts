/**
 * 挖掘算法目录（移植 P2）——能力面的前端唯一读取口。
 *
 * 后端契约面 = `GET /alpha-agent/algorithms`（与 `/mining/config-options` 的
 * `algorithms` 段同源：同一份 manifest 摘要、同一个 `list_algorithms()`）。
 * 本模块**只做字段归一**，不发明默认值：能力面（markets / freqs / uses_llm /
 * uses_datasets / requires_gpu / progress_kind / stages）一律照抄，前端所有
 * 控件显隐都由它驱动——不硬编码算法名，注册表新增算法前端零改动。
 *
 * 坏 manifest（`valid: false`）与合法目录分开返回：它没有可信的能力面，
 * 混进选择器会渲染出一个「什么都不支持」的算法——如实放进 `invalid` 报告。
 */

import { apiClient } from '../../../services/aiStrategyClients';
import type { ApiResponse } from '../types-v2';
import { extractDetail } from './docMiningApi';

export interface MiningAlgorithmStage {
  key: string;
  label: string;
}

export interface MiningAlgorithm {
  key: string;
  name: string;
  version: string;
  desc: string;
  vendor: string;
  enabled: boolean;
  tags: string[];
  /** 支持的市场（市场切换的禁用依据） */
  markets: string[];
  /** 支持的挖掘频率（频率档位的渲染依据；'day' = 日线默认） */
  freqs: string[];
  /** 是否消费数据集选择（false → 面板收起数据集行） */
  usesDatasets: boolean;
  /** 是否消费方向文本 / 提示词池（false → 方向输入/拆解/父本整块不适用） */
  usesLlm: boolean;
  /** 是否依赖 GPU（false = 纯 CPU，后端不跑硬件锁） */
  requiresGpu: boolean;
  /**
   * 进度面形状（manifest 声明）：'workdir' = 演化轮目录扫描（Round x/y 语义），
   * 'jsonl' = 单流水线阶段流。未知值原样透传，消费方按「非 jsonl = workdir」
   * 退化渲染——不猜新语义。
   */
  progressKind: string;
  /** 阶段表（label 由 manifest 给，前端不翻译 key） */
  stages: MiningAlgorithmStage[];
  /** runner 脚本（仓根相对路径；诊断展示用） */
  runner: string;
}

/** 坏 manifest：没有可信能力面，只报告不选择 */
export interface InvalidAlgorithm {
  key: string;
  error: string;
}

export interface MiningAlgorithmCatalog {
  /** 注册表默认 key（后端 `default`）；空串 = 后端没给（前端不猜） */
  defaultKey: string;
  /** 合法 manifest（含 enabled=false：目录如实全给，选择器自行过滤） */
  algorithms: MiningAlgorithm[];
  invalid: InvalidAlgorithm[];
}

function strList(raw: unknown): string[] {
  return Array.isArray(raw)
    ? raw.filter((v): v is string => typeof v === 'string')
    : [];
}

function normalizeAlgorithm(raw: any): MiningAlgorithm | null {
  if (!raw || typeof raw !== 'object' || typeof raw.key !== 'string' || !raw.key) {
    return null;
  }
  return {
    key: raw.key,
    name: typeof raw.name === 'string' && raw.name ? raw.name : raw.key,
    version: typeof raw.version === 'string' ? raw.version : '',
    desc: typeof raw.desc === 'string' ? raw.desc : '',
    vendor: typeof raw.vendor === 'string' ? raw.vendor : '',
    enabled: raw.enabled !== false,
    tags: strList(raw.tags),
    markets: strList(raw.markets),
    freqs: strList(raw.freqs),
    // 能力面缺字段一律按最保守解释（false）：「没声明支持」不是支持
    usesDatasets: raw.uses_datasets === true,
    usesLlm: raw.uses_llm === true,
    requiresGpu: raw.requires_gpu === true,
    progressKind: typeof raw.progress_kind === 'string' ? raw.progress_kind : '',
    stages: Array.isArray(raw.stages)
      ? raw.stages
          .filter((s: any) => typeof s?.key === 'string' && s.key)
          .map((s: any) => ({
            key: s.key,
            label: typeof s.label === 'string' && s.label ? s.label : s.key,
          }))
      : [],
    runner: typeof raw.runner === 'string' ? raw.runner : '',
  };
}

export async function getMiningAlgorithms(): Promise<
  ApiResponse<MiningAlgorithmCatalog>
> {
  try {
    const res = await apiClient.get('/alpha-agent/algorithms');
    const raw = res.data?.data ?? {};
    const algorithms: MiningAlgorithm[] = [];
    const invalid: InvalidAlgorithm[] = [];
    const rows = Array.isArray(raw.algorithms) ? raw.algorithms : [];
    for (const row of rows) {
      if (row?.valid === false) {
        invalid.push({
          key: typeof row.key === 'string' ? row.key : '',
          error:
            typeof row.error === 'string' && row.error
              ? row.error
              : 'manifest 非法',
        });
        continue;
      }
      const algo = normalizeAlgorithm(row);
      if (algo) algorithms.push(algo);
    }
    return {
      success: true,
      data: {
        defaultKey: typeof raw.default === 'string' ? raw.default : '',
        algorithms,
        invalid,
      },
    };
  } catch (error: unknown) {
    return { success: false, error: extractDetail(error) };
  }
}

/** 选择器可选集：合法且启用（invalid 已在归一阶段剔除） */
export function selectableAlgorithms(
  catalog: MiningAlgorithmCatalog | null,
): MiningAlgorithm[] {
  return (catalog?.algorithms ?? []).filter((a) => a.enabled);
}

/**
 * 定位生效算法：显式 key 优先，空 key = 注册表默认。
 * 目录里找不到（任务 key 已下线 / 目录未加载 / 后端没给默认）→ null，
 * 调用方按「能力面未知」处理——不猜、不禁用任何控件。
 */
export function resolveAlgorithm(
  catalog: MiningAlgorithmCatalog | null,
  key: string | null | undefined,
): MiningAlgorithm | null {
  const wanted = (key ?? '').trim() || (catalog?.defaultKey ?? '');
  if (!wanted) return null;
  return (catalog?.algorithms ?? []).find((a) => a.key === wanted) ?? null;
}

/**
 * 市场是否受支持。能力面未知（目录未加载）或**未声明**市场 = 不拦——
 * 「没声明」不是「都不支持」，拿空表禁用所有市场会把人锁死。
 */
export function algorithmSupportsMarket(
  algo: MiningAlgorithm | null,
  market: string,
): boolean {
  return !algo || algo.markets.length === 0 || algo.markets.includes(market);
}

/**
 * 频率是否受支持。''/null 是日线默认语义（与 mining_config 的 isNonDefaultFreq
 * 同一口径），按 'day' 比对——不把「未选择」当成一种独立频率。
 * 未声明频率同市场：不拦。
 */
export function algorithmSupportsFreq(
  algo: MiningAlgorithm | null,
  freq: string | null | undefined,
): boolean {
  if (!algo || algo.freqs.length === 0) return true;
  return algo.freqs.includes((freq ?? '').trim() || 'day');
}
