<template>
  <div class="page-container">
    <div class="page-shell margin-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Coin /></el-icon>融资融券</h2>
        <div class="page-subtitle">统一查看杠杆情绪、异动列表与个股融资融券明细</div>
      </div>
      <div class="hero-chip">
        <el-icon><Coin /></el-icon>
        <span>杠杆情绪观察台</span>
      </div>
    </div>

      <section class="section-block">
        <div class="gauge-row">
          <div class="panel-card gauge-card">
            <div class="panel-title"><el-icon><Odometer /></el-icon>杠杆情绪指数</div>
            <v-chart :option="gaugeOption" style="height: 260px; flex: 1" autoresize />
          </div>
          <div class="metrics-panel margin-metrics-panel index-cards-wrap">
            <div class="index-cards">
              <div class="stat-card margin-stat-card"><div class="metric-head"><el-icon><Wallet /></el-icon><span>融资余额(亿)</span></div><div class="stat-value">{{ indexData.total_margin_balance_yi?.toFixed(0) || '--' }}</div></div>
              <div class="stat-card margin-stat-card"><div class="metric-head"><el-icon><Tickets /></el-icon><span>融券余额(亿)</span></div><div class="stat-value">{{ indexData.total_short_balance_yi?.toFixed(0) || '--' }}</div></div>
              <div class="stat-card margin-stat-card"><div class="metric-head"><el-icon><TrendCharts /></el-icon><span>环比变化</span></div><div class="stat-value" :class="changeColorClass(indexData.margin_change_pct)">{{ formatChange(indexData.margin_change_pct) }}</div></div>
              <div class="stat-card margin-stat-card"><div class="metric-head"><el-icon><Compass /></el-icon><span>杠杆情绪</span></div><div class="stat-value">{{ indexData.sentiment_label || '--' }}</div></div>
            </div>
          </div>
        </div>
      </section>

      <section class="section-block">
      <div class="section-title">融资融券异动</div>
      <div class="panel-card">
      <div class="mobile-table-wrap">
      <el-table :data="anomalies" stripe size="small" empty-text="暂无数据">
        <el-table-column prop="code" label="代码" width="80" />
        <el-table-column prop="name" label="名称" width="80" />
        <el-table-column prop="type" label="类型" width="100">
          <template #default="{ row }"><el-tag :type="row.type.includes('融资') ? 'danger' : 'success'" size="small">{{ row.type }}</el-tag></template>
        </el-table-column>
        <el-table-column prop="margin_buy" label="融资买入" width="100" align="right">
          <template #default="{ row }">{{ formatAmount(row.margin_buy) }}</template>
        </el-table-column>
        <el-table-column prop="margin_balance" label="融资余额" width="100" align="right">
          <template #default="{ row }">{{ formatAmount(row.margin_balance) }}</template>
        </el-table-column>
        <el-table-column prop="margin_change_pct" label="环比" width="90" align="right">
          <template #default="{ row }"><span :class="changeColorClass(row.margin_change_pct)">{{ formatChange(row.margin_change_pct) }}</span></template>
        </el-table-column>
        <el-table-column prop="short_balance" label="融券余额" width="100" align="right">
          <template #default="{ row }">{{ formatAmount(row.short_balance) }}</template>
        </el-table-column>
        <el-table-column prop="detail" label="详情" min-width="200" show-overflow-tooltip />
      </el-table>
      </div>
    </div>
      </section>

      <section class="section-block">
        <div class="section-title">个股融资融券</div>
      <div class="panel-card query-panel">
        <div class="query-row">
          <el-input v-model="queryCode" placeholder="输入股票代码" style="width: 220px" @keyup.enter="queryDetail" />
          <el-button type="primary" @click="queryDetail" :loading="querying">查询</el-button>
        </div>
        <el-descriptions v-if="marginDetail" :column="2" size="small" border>
          <el-descriptions-item label="标记">{{ marginDetail.tag }}</el-descriptions-item>
          <el-descriptions-item label="可交易">{{ marginDetail.is_tradeable ? '是' : '否' }}</el-descriptions-item>
          <el-descriptions-item v-for="(val, key) in marginDetail.factors || {}" :key="key" :label="key">
            {{ typeof val === 'number' ? val.toFixed(4) : val }}
          </el-descriptions-item>
        </el-descriptions>
      </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureGaugeChartsRegistered } from '@/composables/echarts/gauge'
import { getMarginAnomalies, getMarginIndex, getMarginDetail } from '@/api'
import { formatChange, changeColorClass, formatAmount } from '@/composables/useUtils'

ensureGaugeChartsRegistered()

const anomalies = ref([])
const indexData = ref({})
const queryCode = ref('')
const querying = ref(false)
const marginDetail = ref(null)

const gaugeOption = computed(() => {
  const val = indexData.value.leverage_sentiment || 0
  return {
    backgroundColor: 'transparent',
    series: [{
      type: 'gauge', startAngle: 200, endAngle: -20, min: 0, max: 100,
      axisLine: { lineStyle: { width: 18, color: [[0.3, '#b8c3d9'], [0.55, '#22c55e'], [0.7, '#f59e0b'], [1, '#ef4444']] } },
      pointer: { width: 5, itemStyle: { color: '#34507a' } },
      axisTick: { show: false }, splitLine: { length: 8, lineStyle: { width: 2, color: '#cfd8ea' } },
      axisLabel: { distance: 18, color: '#7a8aa0', fontSize: 11 },
      detail: { valueAnimation: true, formatter: '{value}', color: '#34507a', fontSize: 28, offsetCenter: [0, '70%'] },
      data: [{ value: val }],
    }],
  }
})

async function queryDetail() {
  if (!queryCode.value) return
  querying.value = true
  try { marginDetail.value = await getMarginDetail(queryCode.value) } catch { /* ignore */ }
  querying.value = false
}

onMounted(async () => {
  try {
    const [a, i] = await Promise.allSettled([getMarginAnomalies(), getMarginIndex()])
    if (a.status === 'fulfilled') anomalies.value = a.value.anomalies || []
    if (i.status === 'fulfilled') indexData.value = i.value
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.margin-page { display: flex; flex-direction: column; gap: 18px; }
.gauge-row { display: flex; gap: 16px; }
.gauge-card { flex: 1; }
.index-cards-wrap { flex: 0 0 320px; padding: 4px; }
.index-cards { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.margin-stat-card { padding: 16px; background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 14px; box-shadow: var(--claw-shadow-sm); }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
:deep(.el-descriptions) { border-radius: 12px; overflow: hidden; }
@media (max-width: 768px) {
  .gauge-row { flex-direction: column; align-items: stretch; }
  .index-cards-wrap { flex: auto; }
}
</style>
