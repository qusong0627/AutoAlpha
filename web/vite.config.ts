import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

/**
 * 因子挖掘前端独立版（只含 alpha-research + factor-research）。
 *
 * 与主应用 electron/vite.config.ts 的差异：
 *  1. 没有 Electron 相关处理（不需要主进程/预加载/IPC）；
 *  2. 没有 monaco / COS / 多服务端口那一堆 define（本模块用不到）；
 *  3. /api 代理默认指向独立后端的 6789；内部信任头**只在显式提供密钥时**才代填。
 */
const apiTarget = process.env.VITE_API_URL || 'http://localhost:6789';
const basicSecurityHeaders = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'X-Frame-Options': 'DENY',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
};
// Dev mode omits CSP because the Vite/React Refresh bootstrap injects an inline module script.
// The built preview uses external bundles, so it can enforce a script-src 'self' policy.
const previewSecurityHeaders = {
  ...basicSecurityHeaders,
  'Content-Security-Policy': [
    "default-src 'self'",
    "base-uri 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "form-action 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self'",
  ].join('; '),
};

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

// 当前部署是本机免登录开发：dev server 会直接吐源码，且代理可能代填管理员身份。
// 不允许通过 VITE_HOST 把它暴露到局域网；需要远程访问时应先设计独立认证边界。
const devHost = (process.env.VITE_HOST || '127.0.0.1').trim().toLowerCase();
const loopbackHosts = new Set(['127.0.0.1', 'localhost', '::1']);
if (!loopbackHosts.has(devHost)) {
  throw new Error('VITE_HOST 仅支持 loopback（127.0.0.1、localhost 或 ::1）；当前免登录开发服务器不可暴露到局域网。');
}
function isLoopbackAuthority(authority: string): boolean {
  if (!authority || /[\\/@#?\s]/.test(authority)) return false;
  try {
    const parsed = new URL(`http://${authority}`);
    const hostname = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, '');
    return (
      loopbackHosts.has(hostname) &&
      !parsed.username &&
      !parsed.password &&
      parsed.pathname === '/' &&
      !parsed.search &&
      !parsed.hash
    );
  } catch {
    return false;
  }
}

function isSameOriginLoopbackRequest(req: {
  headers: Record<string, string | string[] | undefined>;
  method?: string;
  socket: { encrypted?: boolean };
}): boolean {
  const requestHost = req.headers.host;
  if (typeof requestHost !== 'string' || !isLoopbackAuthority(requestHost)) return false;

  const origin = req.headers.origin;
  const fetchSite = String(req.headers['sec-fetch-site'] || '').toLowerCase();
  if (fetchSite === 'cross-site') return false;
  if (origin === undefined) {
    // A privileged browser proxy must see Origin on every state-changing request.
    // Direct local CLI access can use the backend endpoint instead.
    return ['GET', 'HEAD', 'OPTIONS'].includes((req.method || 'GET').toUpperCase());
  }
  if (typeof origin !== 'string') return false;

  try {
    const parsed = new URL(origin);
    const protocol = req.socket.encrypted ? 'https:' : 'http:';
    return (
      parsed.protocol === protocol &&
      parsed.origin === origin &&
      isLoopbackAuthority(parsed.host) &&
      parsed.host.toLowerCase() === requestHost.toLowerCase()
    );
  } catch {
    return false;
  }
}

function installPrivilegedProxyOriginGuard(server: {
  middlewares: {
    use: (handler: (req: any, res: any, next: () => void) => void) => void;
  };
}): void {
  if (!internalSecret) return;
  server.middlewares.use((req, res, next) => {
    if (!/^\/api(?:\/|\?|$)/.test(req.url || '')) return next();
    if (!isSameOriginLoopbackRequest(req)) {
      res.statusCode = 403;
      res.setHeader('Content-Type', 'text/plain; charset=utf-8');
      res.end('Privileged API proxy requests must come from this local origin.');
      return;
    }
    next();
  });
}

const guardPrivilegedProxyOrigin = {
  name: 'autoalpha-privileged-proxy-origin',
  configureServer: installPrivilegedProxyOriginGuard,
  configurePreviewServer: installPrivilegedProxyOriginGuard,
};
const enforceLoopbackHost = {
  name: 'autoalpha-loopback-host',
  configResolved(config: {
    server: { host?: string | boolean };
    preview: { host?: string | boolean };
  }) {
    // CLI host flags override config values. Preview inherits the /api proxy too,
    // so it must stay local when that proxy may inject the internal credential.
    const resolvedHosts = [
      config.server.host,
      config.preview.host ?? config.server.host,
    ];
    if (resolvedHosts.some(host =>
      typeof host !== 'string' || !loopbackHosts.has(host.toLowerCase())
    )) {
      throw new Error('Vite dev/preview server 仅允许 loopback 绑定；请移除非本机的 --host 参数。');
    }
  },
};

export default defineConfig({
  plugins: [react(), guardPrivilegedProxyOrigin, enforceLoopbackHost],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  define: {
    'process.env.NODE_ENV': JSON.stringify(process.env.NODE_ENV || 'development'),
  },
  server: {
    port: parseInt(process.env.VITE_PORT || '1688'),
    strictPort: true,
    host: devHost,
    headers: basicSecurityHeaders,
    proxy: {
      '/health': {
        target: apiTarget,
        changeOrigin: true,
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
        target: process.env.VITE_WS_URL || 'ws://localhost:6789',
        ws: true,
      },
    },
  },
  preview: {
    host: devHost,
    headers: previewSecurityHeaders,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
});
