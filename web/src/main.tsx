import React from 'react';
import ReactDOM from 'react-dom/client';
import { HashRouter } from 'react-router-dom';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import 'antd/dist/reset.css';
import App from './App';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode><ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#4f63f3', borderRadius: 10, colorText: '#26334f', colorTextSecondary: '#7b88a2', colorBorder: '#e1e7f3', fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif', fontSize: 13, controlHeight: 38 }, components: { Table: { headerBg: '#f7f9fe', headerColor: '#8591a8', borderColor: '#edf0f6', cellPaddingBlock: 15 }, Modal: { borderRadiusLG: 20 }, Drawer: { colorBgElevated: '#fbfcff' }, Button: { primaryShadow: '0 4px 12px rgba(79,99,243,.15)' } } }}><AntApp><HashRouter><App /></HashRouter></AntApp></ConfigProvider></React.StrictMode>,
);
