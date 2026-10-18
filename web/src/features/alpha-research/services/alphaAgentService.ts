import { apiClient } from '../../../services/aiStrategyClients';

const ALPHA_AGENT_BASE = '/alpha-agent';

export interface MarketInfo {
  market_id: string;
  market_name: string;
  description: string;
  data_ready: boolean;
}

export const alphaAgentService = {
  listMarkets: async (): Promise<MarketInfo[]> => {
    const res = await apiClient.get(`${ALPHA_AGENT_BASE}/markets`);
    return res.data.data.markets;
  },

  promoteByExpression: async (
    factors: Array<{ name: string; expression: string }>,
  ): Promise<{
    success: boolean;
    promoted: Array<{
      factor_name: string;
      feature_key: string;
      expression: string;
      non_null_values: number;
    }>;
    errors: Array<{ factor_name: string; error: string }>;
  }> => {
    const res = await apiClient.post(`/admin/alpha-factors/promote-by-expression`, {
      factors,
    });
    return res.data;
  },
};
