<template>
  <div class="page-container">
    <h2 class="page-title">💰 融资融券</h2>

    <!-- 杠杆情绪指数 -->
    <div class="gauge-row">
      <v-chart :option="gaugeOption" style="height: 260px; flex: 1" autoresize />
      <div class="index-cards">
        <div class="stat-card"><div class="stat-label">融资余额(亿)</div><div class="stat-value">{{ indexData.total_margin_balance_yi?.toFixed(0) || '--' }}</div></div>
        <div class="stat-card"><div class="stat-label">融券余额(亿)</div><div class="stat-value">{{ indexData.total_short_balance_yi?.toFixed(0) || '--' }}</div></div>
        <div class="stat-card"><div class="stat-label">环比变化</div><div class="stat-value" :class="changeColorClass(indexData.margin_change_pct)">{{ formatChange(indexData.margin_change_pct) }}</div></div>
        <div class="stat-card">
          <div class="stat-label">杠杆情绪</div>
          <div class="stat-value">{{ indexData.sentiment_label || '--' }}</div>
        </div>
      </div>
    </div>

    <!-- 异动列表 -->
    <div class="section-title">融资融券异动</div>
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

    <!-- 个股查询 -->
    <div class="section-title">个股融资融券</div>
    <div class="query-row">
      <el-input v-model="queryCode" placeholder="输入股票代码" style="width: 200px" @keyup.enter="queryDetail" />
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
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import { getMarginAnomalies, getMarginIndex, getMarginDetail } from '@/api'
import { formatChange, changeColorClass, formatAmount } from '@/composables/useUtils'

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
      axisLine: { lineStyle: { width: 18, color: [[0.3, '#909399'], [0.55, '#22c55e'], [0.7, '#f59e0b'], [1, '#ef4444']] } },
      pointer: { width: 5, itemStyle: { color: '#1d2939' } },
      axisTick: { show: false }, splitLine: { length: 8, lineStyle: { width: 2, color: '#999' } },
      axisLabel: { distance: 18, color: '#667085', fontSize: 11 },
      detail: { valueAnimation: true, formatter: '{value}', color: '#1d2939', fontSize: 28, offsetCenter: [0, '70%'] },
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
.gauge-row { display: flex; gap: 16px; margin-bottom: 20px; }
.index-cards { flex: 0 0 280px; display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; }
@media (max-width: 768px) {
  .gauge-row { flex-direction: column; }
  .index-cards { flex: auto; }
}
</style>
