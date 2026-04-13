<template>
  <div class="page-container">
    <h2 class="page-title">📈 晋级预测</h2>

    <!-- 顶部统计 -->
    <div class="stat-row">
      <div class="stat-card">
        <div class="stat-label">连板高度</div>
        <div class="stat-value text-red">{{ boardHeight.height || '--' }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">涨停数</div>
        <div class="stat-value">{{ boardHeight.limit_up_count || '--' }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">封板率</div>
        <div class="stat-value">{{ boardHeight.seal_rate ? boardHeight.seal_rate.toFixed(1) + '%' : '--' }}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">晋级率</div>
        <div class="stat-value text-yellow">{{ boardHeight.promotion_rate ? (boardHeight.promotion_rate * 100).toFixed(1) + '%' : '--' }}</div>
      </div>
    </div>

    <!-- 连板梯队 -->
    <div class="section-title">连板梯队</div>
    <v-chart :option="ladderChartOption" style="height: 350px" autoresize />

    <el-table :data="ladderData" stripe size="small" empty-text="暂无数据" row-key="consecutive_days"
      :row-class-name="({ row }) => row.consecutive_days >= 4 ? 'high-ladder' : ''">
      <el-table-column prop="consecutive_days" label="连板" width="70" align="center">
        <template #default="{ row }">
          <strong :class="row.consecutive_days >= 5 ? 'text-red' : row.consecutive_days >= 3 ? 'text-yellow' : ''">
            {{ row.consecutive_days }}板
          </strong>
        </template>
      </el-table-column>
      <el-table-column prop="count" label="个数" width="60" align="center" />
      <el-table-column prop="seal_rate" label="封板率" width="80" align="center">
        <template #default="{ row }">{{ (row.seal_rate * 100).toFixed(1) }}%</template>
      </el-table-column>
      <el-table-column label="个股" min-width="300">
        <template #default="{ row }">
          <el-tag v-for="s in row.stocks?.slice(0, 5)" :key="s.code" size="small" class="stock-tag"
            @click="$router.push(`/stocks/${s.code}`)">
            {{ s.name }}({{ s.code }})
          </el-tag>
        </template>
      </el-table-column>
    </el-table>

    <!-- 晋级概率查询 -->
    <div class="section-title">晋级概率查询</div>
    <div class="query-row">
      <el-input v-model="queryCode" placeholder="输入股票代码" style="width: 200px" @keyup.enter="queryProbability" />
      <el-button type="primary" @click="queryProbability" :loading="querying">查询</el-button>
    </div>

    <div v-if="promotionResult" class="result-card">
      <div class="result-row">
        <span>当前连板: <strong class="text-red">{{ promotionResult.current_days }}板</strong></span>
        <span>→ 晋级: <strong class="text-yellow">{{ promotionResult.next_days }}板</strong></span>
        <span>晋级概率: <strong :class="promotionResult.probability >= 0.5 ? 'text-red' : 'text-green'">{{ (promotionResult.probability * 100).toFixed(1) }}%</strong></span>
        <span>置信度: {{ (promotionResult.confidence * 100).toFixed(0) }}%</span>
      </div>
      <el-progress :percentage="promotionResult.probability * 100" :color="promotionResult.probability >= 0.5 ? '#ef4444' : '#22c55e'" :stroke-width="12" />
      <div v-if="promotionResult.factors" class="factors-list">
        <div v-for="(val, key) in promotionResult.factors" :key="key" class="factor-item">
          <span class="factor-name">{{ key }}</span>
          <span class="factor-val">{{ typeof val === 'number' ? val.toFixed(3) : val }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import { getPromotionLadder, getBoardHeight, getPromotionProbability } from '@/api'

const ladderData = ref([])
const boardHeight = ref({})
const queryCode = ref('')
const querying = ref(false)
const promotionResult = ref(null)

const ladderChartOption = computed(() => {
  const items = [...ladderData.value].reverse()
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 40, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => `${i.consecutive_days}板`), axisLine: { lineStyle: { color: '#e8ebf0' } }, axisLabel: { color: '#667085' } },
    yAxis: { type: 'value', axisLine: { lineStyle: { color: '#e8ebf0' } }, axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    series: [{
      type: 'bar',
      data: items.map(i => ({
        value: i.count,
        itemStyle: { color: i.seal_rate >= 0.8 ? '#ef4444' : i.seal_rate >= 0.5 ? '#f59e0b' : '#22c55e' },
      })),
      barWidth: 30,
      label: { show: true, position: 'top', color: '#667085' },
    }],
  }
})

async function queryProbability() {
  if (!queryCode.value) return
  querying.value = true
  try {
    promotionResult.value = await getPromotionProbability(queryCode.value)
  } catch { /* ignore */ }
  querying.value = false
}

onMounted(async () => {
  try {
    const [l, h] = await Promise.allSettled([getPromotionLadder(), getBoardHeight()])
    if (l.status === 'fulfilled') ladderData.value = l.value.ladder || []
    if (h.status === 'fulfilled') boardHeight.value = h.value
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; }
.result-card {
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: 8px;
  padding: 16px;
}
.result-row { display: flex; gap: 24px; margin-bottom: 12px; font-size: 15px; }
.factors-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 8px; margin-top: 12px; }
.factor-item { display: flex; justify-content: space-between; font-size: 12px; padding: 4px 8px; background: #f5f6fa; border: 1px solid #e8ebf0; border-radius: 4px; }
.factor-name { color: var(--claw-text-secondary); }
.stock-tag { cursor: pointer; margin: 2px; }
:deep(.high-ladder) { background: rgba(239, 68, 68, 0.08) !important; }
@media (max-width: 768px) { .stat-row { grid-template-columns: repeat(2, 1fr); } }
</style>
