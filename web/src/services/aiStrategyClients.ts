// AI 策略服务共享 HTTP 客户端
import axios from 'axios';
import { authService } from '../features/auth/services/authService';
import { SERVICE_ENDPOINTS, SERVICE_URLS } from '../config/services';

function describeRequestFailure(error: unknown): {
  method?: string;
  status?: number;
  code?: string;
} {
  const value = error as {
    config?: { method?: string };
    response?: { status?: number };
    code?: string;
  };
  return {
    method: value?.config?.method?.toUpperCase(),
    status: value?.response?.status,
    code: value?.code,
  };
}

// 改为动态解析函数，确保用户配置 IP 后能实时生效
// 创建axios实例
export const apiClient = axios.create({
  timeout: 300000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 创建回测服务专用客户端
export const backtestClient = axios.create({
  timeout: 180000,
  headers: {
    'Content-Type': 'application/json',
  },
});

// 动态解析函数定义
const resolveAiStrategyBaseURL = () => String(SERVICE_ENDPOINTS.AI_STRATEGY || '').replace(/\/+$/, '');
const resolveBacktestBaseURL = () => String(SERVICE_URLS.ENGINE_SERVICE || '').replace(/\/+$/, '');

apiClient.interceptors.request.use(
  (config) => {
    config.baseURL = resolveAiStrategyBaseURL();
    apiClient.defaults.baseURL = config.baseURL;
    const token = authService.getAccessToken();
    if (token) {
      if (config.headers && typeof config.headers.set === 'function') {
        config.headers.set('Authorization', `Bearer ${token}`);
      } else {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    console.log('API请求:', config.method?.toUpperCase());
    return config;
  },
  (error) => {
    console.error('API请求错误:', describeRequestFailure(error));
    return Promise.reject(error);
  }
);

apiClient.interceptors.response.use(
  (response) => {
    console.log('API响应:', response.status);
    return response;
  },
  (error) => {
    console.error('API响应错误:', describeRequestFailure(error));
    if (error.response?.status === 401) {
      console.error('认证失败，请检查认证状态');
    }
    return Promise.reject(error);
  }
);

backtestClient.interceptors.request.use(
  (config) => {
    config.baseURL = resolveBacktestBaseURL();
    backtestClient.defaults.baseURL = config.baseURL;
    const token = authService.getAccessToken();
    if (token) {
      if (config.headers && typeof config.headers.set === 'function') {
        config.headers.set('Authorization', `Bearer ${token}`);
      } else {
        config.headers.Authorization = `Bearer ${token}`;
      }
    }
    return config;
  }
);
