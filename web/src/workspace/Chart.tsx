import type { CSSProperties } from 'react';
import * as echarts from 'echarts/core';
import { BarChart, GraphChart, LineChart } from 'echarts/charts';
import { GridComponent, LegendComponent, TooltipComponent } from 'echarts/components';
import { CanvasRenderer } from 'echarts/renderers';
import ReactEChartsCore from 'echarts-for-react/lib/core';

// ECharts 按需注册：每种 series 类型都必须 use，否则该系列静默不画
// （IC 序列用 bar —— 漏注册会得到「有坐标轴、无数据」的空图）。
echarts.use([LineChart, BarChart, GraphChart, GridComponent, LegendComponent, TooltipComponent, CanvasRenderer]);

export default function Chart({ option, style = { height: 320 } }: { option: Record<string, any>; style?: CSSProperties }) {
  return <ReactEChartsCore echarts={echarts} option={option} style={style} notMerge lazyUpdate />;
}
