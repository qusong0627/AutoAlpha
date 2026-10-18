import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

/**
 * 因子挖掘前端独立版（只含 alpha-research + factor-research）。
 *
 * 与主应用 electron/vite.config.ts 的差异：
 *  1. 没有 Electron 相关处理（不需要主进程/预加载/IPC）；
 *  2. 没有 monaco / COS / 多服务端口那一堆 define（本模块用不到）；
 *  3. /api 代理默认指向独立后端的 8001；内部信任头**只在显式提供密钥时**才代填。
 */
const apiTarget = process.env.VITE_API_URL || 'http://localhost:8001';

// ── 内部信任头（安全相关，改前务必读完这段）────────────────────────────
// 本文件此前给 INTERNAL_CALL_SECRET 加过一个硬编码兜底：一个长期写在仓库里的
// "开发用"密钥（现已退役，并已列入后端 `_PUBLIC_INTERNAL_DEFAULTS` 黑名单，
// 见 backend/shared/auth.py）。当它**不在**该黑名单里时，后端会把它当作真实
// 密钥接受；配合下面的 host，任何能访问本 dev server 的人都会自动获得
// user_id=10000001 的管理员身份——**即使后端已设 AUTH_DISABLED=0**。
// 现在：密钥只从环境变量取、没有默认值；未提供时不注入任何身份头
// （= 不授予任何权限，后端照常鉴权，该 401 就 401）。
const internalSecret = (process.env.INTERNAL_CALL_SECRET || '').trim();
const devUserId = process.env.DEV_USER_ID || '10000001';
const devTenantId = process.env.DEV_TENANT_ID || 'default';

if (!internalSecret) {
  console.warn(
    '[vite] INTERNAL_CALL_SECRET 未设置：/api 代理不会注入内部身份头。\n' +
      '       后端 AUTH_DISABLED=1 时无影响；AUTH_DISABLED=0 时请求会 401。\n' +
      '       确需代填请先 export INTERNAL_CALL_SECRET=<与后端一致的真实密钥>。'
  );
}

// 只监听回环：dev server 会直接吐源码，且代理会代填身份，不应暴露到局域网。
// 确需局域网访问时显式 export VITE_HOST=0.0.0.0，并自担风险。
const devHost = process.env.VITE_HOST || '127.0.0.1';

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  define: {
    'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV || 'development'),
    'process.env.VITE_API_BASE_URL': JSON.stringify(process.env.VITE_API_BASE_URL || apiTarget),
  },
  server: {
    port: parseInt(process.env.VITE_PORT || '3000'),
    strictPort: true,
    host: devHost,
    proxy: {
      '/backend-health': {
        target: apiTarget,
        changeOrigin: true,
        rewrite: () => '/health',
      },
      '/api': {
        target: apiTarget,
        changeOrigin: true,
        configure: (proxy) => {
          // 没有密钥就不注入任何身份头（fail-closed）。
          if (!internalSecret) return;
          proxy.on('proxyReq', (proxyReq) => {
            proxyReq.setHeader('X-Internal-Call', internalSecret);
            proxyReq.setHeader('X-User-Id', devUserId);
            proxyReq.setHeader('X-Tenant-Id', devTenantId);
          });
        },
      },
      '/ws': {
        target: process.env.VITE_WS_URL || 'ws://localhost:8001',
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
