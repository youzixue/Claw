<template>
  <div class="page-container">
    <h2 class="page-title">☀️ 情绪面</h2>

    <!-- 情绪周期仪表盘 -->
    <div class="gauge-row">
      <v-chart :option="gaugeOption" style="height: 280px; flex: 1" autoresize />
      <div class="advice-card">
        <div class="section-title">仓位建议</div>
        <div class="advice-text">{{ sentimentData.position_advice || '暂无建议' }}</div>
        <div class="section-title" style="margin-top:12px">操作建议</div>
        <div class="advice-text">{{ sentimentData.suggestion || '暂无' }}</div>
      </div>
    </div>

    <!-- 市场统计 -->
    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-label">情绪周期</div>
        <div class="stat-value" :style="{ color: sentimentCycleColor(sentimentData.cycle) }">
          {{ sentimentCycleLabel(sentimentData.cycle) }}
        </div>
      </div>
      <div class="stat-card">
        <div class="stat-label">涨停数</div>
        <div class="stat-value text-red">{{ stats.limit_up_count ?? '--' }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">跌停数</div>
        <div class="stat-value text-green">{{ stats.limit_down_count ?? '--' }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">封板率</div>
        <div class="stat-value">{{ stats.seal_rate != null ? stats.seal_rate.toFixed(1) + '%' : '--' }}</div>
      </div>
    </div>

    <!-- 情绪历史趋势 -->
    <div class="section-title">情绪历史趋势</div>
    <v-chart :option="historyChartOption" style="height: 320px" autoresize />
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import { getSentimentCycle, getSentimentStats, getSentimentHistory } from '@/api'
import { sentimentCycleLabel, sentimentCycleColor } from '@/composables/useUtils'

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
          color: [[0.25, '#909399'], [0.5, '#67c23a'], [0.75, '#e6a23c'], [1, '#f56c6c']],
        },
      },
      pointer: { width: 5, length: '60%', itemStyle: { color: '#1d2939' } },
      axisTick: { show: false },
      splitLine: { length: 10, lineStyle: { width: 2, color: '#999' } },
      axisLabel: { distance: 20, color: '#667085', fontSize: 11 },
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
    legend: { data: ['情绪分数', '涨停数'], textStyle: { color: '#667085' } },
    grid: { left: 60, right: 60, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.trade_date?.slice(5) || ''), axisLabel: { color: '#667085' } },
    yAxis: [
      { type: 'value', name: '分数', axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
      { type: 'value', name: '涨停', axisLabel: { color: '#667085' }, splitLine: { show: false } },
    ],
    series: [
      { name: '情绪分数', type: 'line', data: items.map(i => i.score), smooth: true, lineStyle: { color: '#06b6d4' }, itemStyle: { color: '#06b6d4' } },
      { name: '涨停数', type: 'bar', yAxisIndex: 1, data: items.map(i => i.limit_up_count), itemStyle: { color: '#ef4444', opacity: 0.6 } },
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
.gauge-row { display: flex; gap: 16px; margin-bottom: 20px; }
.advice-card {
  flex: 0 0 280px;
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: 8px;
  padding: 16px;
}
.advice-text { color: var(--claw-text-secondary); font-size: 13px; line-height: 1.6; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
@media (max-width: 768px) {
  .gauge-row { flex-direction: column; }
  .stat-row { grid-template-columns: repeat(2, 1fr); }
}
</style>
