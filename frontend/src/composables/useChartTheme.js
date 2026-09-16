/**
 * ECharts 浅色轻奢主题配色
 * 统一管理所有图表的颜色，避免在各组件中硬编码
 */

// 文字色
export const CHART_TEXT = '#667085'       // 次要文字 (axisLabel, legend)
export const CHART_TEXT_PRIMARY = '#1d2939' // 主要文字 (title, detail)
export const CHART_TEXT_MUTED = '#98a2b3'   // 淡文字

// 线/边框
export const CHART_AXIS_LINE = '#e8ebf0'    // 轴线
export const CHART_SPLIT_LINE = '#f0f2f5'   // 分割线

// 区域填充
export const CHART_AREA_LIGHT = 'rgba(79, 110, 247, 0.08)'
export const CHART_AREA_MEDIUM = 'rgba(79, 110, 247, 0.15)'

// A股颜色
export const CHART_RED = '#e84142'
export const CHART_GREEN = '#1db954'
export const CHART_YELLOW = '#f7a528'
export const CHART_BLUE = '#4f6ef7'
export const CHART_PURPLE = '#8b5cf6'
export const CHART_ORANGE = '#f97316'
export const CHART_CYAN = '#0ea5e9'

// 饼图配色
export const PIE_COLORS = ['#4f6ef7', '#e84142', '#f7a528', '#1db954', '#8b5cf6', '#0ea5e9', '#f97316']

// 雷达图区域填充
export const RADAR_AREA = ['rgba(79, 110, 247, 0.05)', 'rgba(79, 110, 247, 0.1)']

// 通用 tooltip 配置
export const TOOLTIP = {
  trigger: 'axis',
  backgroundColor: '#fff',
  borderColor: '#e8ebf0',
  borderWidth: 1,
  textStyle: { color: '#1d2939', fontSize: 12 },
  extraCssText: 'box-shadow: 0 4px 12px rgba(0,0,0,0.08); border-radius: 8px;',
}

// 通用 grid 配置
export const GRID = { left: 60, right: 20, top: 40, bottom: 30 }

// 通用 xAxis
export const xAxisCategory = (data) => ({
  type: 'category',
  data,
  axisLine: { lineStyle: { color: CHART_AXIS_LINE } },
  axisLabel: { color: CHART_TEXT, fontSize: 11 },
  axisTick: { show: false },
})

// 通用 yAxis
export const yAxisValue = (name) => ({
  type: 'value',
  name: name || '',
  nameTextStyle: { color: CHART_TEXT, fontSize: 11 },
  axisLine: { show: false },
  axisLabel: { color: CHART_TEXT, fontSize: 11 },
  splitLine: { lineStyle: { color: CHART_SPLIT_LINE, type: 'dashed' } },
})

// 通用 legend
export const legend = (data) => ({
  data,
  textStyle: { color: CHART_TEXT, fontSize: 12 },
  itemWidth: 12,
  itemHeight: 8,
  itemGap: 16,
})
