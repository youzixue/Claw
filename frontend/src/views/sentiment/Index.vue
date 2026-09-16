<template>
  <div class="page-container">
    <div class="page-shell sentiment-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Sunny /></el-icon>情绪面</h2>
        <div class="page-subtitle">情绪周期、仓位建议与历史波动的统一观察面板</div>
      </div>
      <div class="hero-chip">
        <el-icon><DataAnalysis /></el-icon>
        <span>自动读取最新统计</span>
      </div>
    </div>

      <section class="section-block">
        <div class="gauge-row">
          <div class="chart-card panel-card gauge-card">
            <div class="card-head">
              <div class="card-title"><el-icon><Odometer /></el-icon>情绪周期仪表盘</div>
            </div>
            <v-chart :option="gaugeOption" style="height: 280px; flex: 1" autoresize />
          </div>

          <div class="advice-card panel-card">
            <div class="advice-block">
              <div class="advice-kicker"><el-icon><Coin /></el-icon><span>仓位建议</span></div>
              <div class="advice-text">{{ sentimentData.position_advice || '暂无建议' }}</div>
            </div>
            <div class="advice-block">
              <div class="advice-kicker"><el-icon><Opportunity /></el-icon><span>操作建议</span></div>
              <div class="advice-text">{{ sentimentData.suggestion || '暂无' }}</div>
            </div>
          </div>
        </div>
      </section>

      <section class="section-block">
        <div class="section-title">情绪核心指标</div>
        <div class="metrics-panel sentiment-metrics-panel">
          <div class="stat-row">
            <div class="stat-card sentiment-stat-card">
              <div class="metric-head"><el-icon><Sunny /></el-icon><span>情绪周期</span></div>
              <div class="stat-value" :style="{ color: sentimentCycleColor(sentimentData.cycle) }">
                {{ sentimentCycleLabel(sentimentData.cycle) }}
              </div>
            </div>
            <div class="stat-card sentiment-stat-card">
              <div class="metric-head"><el-icon><Top /></el-icon><span>涨停数</span></div>
              <div class="stat-value text-red">{{ stats.limit_up_count ?? '--' }}</div>
            </div>
            <div class="stat-card sentiment-stat-card">
              <div class="metric-head"><el-icon><Bottom /></el-icon><span>跌停数</span></div>
              <div class="stat-value text-green">{{ stats.limit_down_count ?? '--' }}</div>
            </div>
            <div class="stat-card sentiment-stat-card">
              <div class="metric-head"><el-icon><Finished /></el-icon><span>封板率</span></div>
              <div class="stat-value">{{ stats.seal_rate != null ? stats.seal_rate.toFixed(1) + '%' : '--' }}</div>
            </div>
          </div>
        </div>
      </section>

      <section class="section-block">
        <div class="section-title">情绪历史趋势</div>
        <div class="chart-card panel-card history-card">
          <div class="card-head">
            <div class="card-title"><el-icon><TrendCharts /></el-icon>情绪历史趋势</div>
          </div>
          <v-chart :option="historyChartOption" style="height: 320px" autoresize />
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureGaugeChartsRegistered } from '@/composables/echarts/gauge'
import { ensureLineBarChartsRegistered } from '@/composables/echarts/line-bar'
import { getSentimentCycle, getSentimentStats, getSentimentHistory } from '@/api'
import { sentimentCycleLabel, sentimentCycleColor } from '@/composables/useUtils'

ensureGaugeChartsRegistered()
ensureLineBarChartsRegistered()

const sentimentData = ref({})
const stats = ref({})
const historyData = ref([])

const gaugeOption = computed(() => {
  const score = sentimentData.value.score || 0
  const cycle = sentimentData.value.cycle || 'pending'
  return {
    backgroundColor: 'transparent',
    series: [{
      type: 'gauge',
      startAngle: 200, endAngle: -20,
      min: 0, max: 100,
      axisLine: {
        lineStyle: {
          width: 20,
          color: [[0.25, '#b8c3d9'], [0.5, '#67c23a'], [0.75, '#e6a23c'], [1, '#f56c6c']],
        },
      },
      pointer: { width: 5, length: '60%', itemStyle: { color: '#34507a' } },
      axisTick: { show: false },
      splitLine: { length: 10, lineStyle: { width: 2, color: '#cfd8ea' } },
      axisLabel: { distance: 20, color: '#7a8aa0', fontSize: 11 },
      detail: {
        valueAnimation: true, formatter: `{value}\n${sentimentCycleLabel(cycle)}`,
        color: sentimentCycleColor(cycle), fontSize: 20, offsetCenter: [0, '70%'],
      },
      data: [{ value: score }],
    }],
  }
})

const historyChartOption = computed(() => {
  const items = historyData.value
  if (!items.length) return { backgroundColor: 'transparent' }
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: ['情绪分数', '涨停数'], textStyle: { color: '#7a8aa0' } },
    grid: { left: 60, right: 60, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.trade_date?.slice(5) || ''), axisLabel: { color: '#7a8aa0' } },
    yAxis: [
      { type: 'value', name: '分数', axisLabel: { color: '#7a8aa0' }, splitLine: { lineStyle: { color: '#edf2fb' } } },
      { type: 'value', name: '涨停', axisLabel: { color: '#7a8aa0' }, splitLine: { show: false } },
    ],
    series: [
      { name: '情绪分数', type: 'line', data: items.map(i => i.score), smooth: true, lineStyle: { color: '#007aff', width: 2 }, itemStyle: { color: '#007aff' }, areaStyle: { color: 'rgba(0,122,255,0.08)' } },
      { name: '涨停数', type: 'bar', yAxisIndex: 1, data: items.map(i => i.limit_up_count), itemStyle: { color: 'rgba(239,68,68,0.65)', borderRadius: [4, 4, 0, 0] } },
    ],
  }
})

onMounted(async () => {
  try {
    const [c, s, h] = await Promise.allSettled([getSentimentCycle(), getSentimentStats(), getSentimentHistory({ days: 30 })])
    if (c.status === 'fulfilled') sentimentData.value = c.value
    if (s.status === 'fulfilled') stats.value = s.value.stats || {}
    if (h.status === 'fulfilled') historyData.value = h.value.history || []
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.sentiment-page { display: flex; flex-direction: column; gap: 18px; }
.gauge-row { display: flex; gap: 16px; }
.sentiment-metrics-panel { padding: 4px; }
.sentiment-stat-card { background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 14px; box-shadow: var(--claw-shadow-sm); }
.gauge-card { flex: 1; padding: 14px 16px 8px; }
.card-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
.card-title, .section-title, .metric-head { display: inline-flex; align-items: center; gap: 8px; }
.card-title { font-size: 14px; font-weight: 600; color: var(--claw-text); }
.advice-card { flex: 0 0 320px; padding: 16px; display: flex; flex-direction: column; gap: 18px; }
.advice-block { padding: 12px; border-radius: 12px; background: rgba(245, 247, 250, 0.82); border: 1px solid var(--claw-border-light); }
.advice-kicker { display: inline-flex; align-items: center; gap: 8px; color: var(--claw-text-secondary); font-size: 13px; font-weight: 600; }
.advice-text { color: var(--claw-text-secondary); font-size: 13px; line-height: 1.7; margin-top: 10px; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 0; }
.sentiment-stat-card { padding: 16px; }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.history-card { padding: 14px 16px 8px; }
@media (max-width: 768px) {
  .gauge-row { flex-direction: column; align-items: stretch; }
  .advice-card { flex: 1 1 auto; width: 100%; }
  .stat-row { grid-template-columns: repeat(2, 1fr); }
}
</style>
