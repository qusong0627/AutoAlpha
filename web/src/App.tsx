import { Component, Suspense, lazy, type ReactNode } from 'react';
import { Navigate, NavLink, Route, Routes } from 'react-router-dom';
import { Alert, Button, Spin, Tooltip } from 'antd';
import { Sparkles } from 'lucide-react';
import { WorkspaceProvider, useWorkspace } from './workspace/WorkspaceContext';
import './workspace/workspace.css';

const HomePage = lazy(() => import('./workspace/HomePage'));
const MiningPage = lazy(() => import('./workspace/MiningPage'));
const TasksPage = lazy(() => import('./workspace/TasksPage'));
const FactorsPage = lazy(() => import('./workspace/FactorsPage'));
const BacktestPage = lazy(() => import('./workspace/BacktestPage'));
const HelpPage = lazy(() => import('./workspace/HelpPage'));
class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() { return this.state.failed ? <div className="workspace-page"><Alert type="error" showIcon message="页面加载失败" description="请重新加载页面后重试。" action={<Button onClick={() => window.location.reload()}>重新加载</Button>} /></div> : this.props.children; }
}
function Shell() {
  const { service } = useWorkspace();
  const connected = service.data?.status === 'healthy' && !service.error;
  const status = service.error ? '服务连接异常 · 重试' : connected ? '本地服务已连接' : '正在连接后端';
  return <div className="workspace-app">
    <a className="skip-link" href="#workspace-main">跳转到内容</a>
    <header className="app-bar">
      <div className="app-bar-inner">
        <div className="app-bar-top">
          <NavLink to="/" className="brand" aria-label="返回首页">
            <span className="brand-symbol"><Sparkles size={21} /></span>
            <span className="brand-text"><strong>AutoAlpha</strong><span>智能因子研究平台</span></span>
          </NavLink>
          <Tooltip title={service.error || (connected ? '后端路由已挂载，业务数据以各页面检查结果为准' : '正在检查本地后端服务')}><button className="connection" onClick={service.refresh}><span className={`status-dot ${connected ? 'online' : service.error ? 'offline' : ''}`} />{status}</button></Tooltip>
        </div>
        <nav className="app-tabs" aria-label="主要功能">
          <NavLink to="/" end className={({ isActive }) => `app-tab${isActive ? ' active' : ''}`}>首页</NavLink>
          <NavLink to="/mine" className={({ isActive }) => `app-tab${isActive ? ' active' : ''}`}>参数配置</NavLink>
          <NavLink to="/tasks" className={({ isActive }) => `app-tab${isActive ? ' active' : ''}`}>任务中心</NavLink>
          <NavLink to="/factors" className={({ isActive }) => `app-tab${isActive ? ' active' : ''}`}>因子库</NavLink>
          <NavLink to="/backtest" className={({ isActive }) => `app-tab${isActive ? ' active' : ''}`}>回测中心</NavLink>
          <NavLink to="/help" className={({ isActive }) => `app-tab${isActive ? ' active' : ''}`}>帮助中心</NavLink>
        </nav>
      </div>
    </header>
    <main id="workspace-main"><PageBoundary><Suspense fallback={<div className="page-loading"><Spin size="large" /><p>正在加载工作空间</p></div>}><Routes>
      <Route path="/" element={<HomePage />} /><Route path="/mine" element={<MiningPage />} /><Route path="/tasks" element={<TasksPage />} /><Route path="/factors" element={<FactorsPage />} /><Route path="/backtest" element={<BacktestPage />} /><Route path="/help" element={<HelpPage />} />
      <Route path="/pool" element={<Navigate to="/factors" replace />} /><Route path="/combos" element={<Navigate to="/backtest" replace />} /><Route path="/research" element={<Navigate to="/factors" replace />} /><Route path="/data" element={<Navigate to="/mine" replace />} /><Route path="/settings" element={<Navigate to="/mine" replace />} /><Route path="/me" element={<Navigate to="/mine" replace />} /><Route path="/alpha-research" element={<Navigate to="/" replace />} /><Route path="/factor-research" element={<Navigate to="/factors" replace />} /><Route path="*" element={<Navigate to="/" replace />} />
    </Routes></Suspense></PageBoundary></main>
    <footer className="workspace-footer">AutoAlpha <span>从假设到证据，让研究持续积累。</span></footer>
  </div>;
}
export default function App() { return <WorkspaceProvider><Shell /></WorkspaceProvider>; }
