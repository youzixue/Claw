<template>
  <div class="page-container">
    <h2 class="page-title">🏆 绩效中心</h2>

    <!-- 信号统计 -->
    <div class="stat-row">
      <div class="stat-card"><div class="stat-label">总信号数</div><div class="stat-value">{{ signalStats.total || '--' }}</div></div>
      <div class="stat-card"><div class="stat-label">胜率</div><div class="stat-value" :class="signalStats.win_rate >= 0.5 ? 'text-red' : 'text-green'">{{ signalStats.win_rate ? (signalStats.win_rate * 100).toFixed(1) + '%' : '--' }}</div></div>
      <div class="stat-card"><div class="stat-label">平均收益</div><div class="stat-value" :class="changeColorClass(signalStats.avg_return)">{{ signalStats.avg_return ? formatChange(signalStats.avg_return) : '--' }}</div></div>
      <div class="stat-card"><div class="stat-label">最大回撤</div><div class="stat-value text-green">{{ signalStats.max_drawdown ? signalStats.max_drawdown.toFixed(2) + '%' : '--' }}</div></div>
    </div>

    <el-tabs v-model="activeTab" >
      <!-- 信号类型分布 -->
      <el-tab-pane label="信号分布" name="distribution">
        <v-chart :option="pieOption" style="height: 350px" autoresize />
      </el-tab-pane>

      <!-- 因子评估 -->
      <el-tab-pane label="因子评估" name="factor-eval">
        <el-table :data="factorEvalList" stripe size="small" empty-text="暂无数据">
          <el-table-column prop="factor_name" label="因子" min-width="150" />
          <el-table-column prop="ic_mean" label="IC" width="80" align="right">
            <template #default="{ row }">{{ row.ic_mean?.toFixed(4) || '--' }}</template>
          </el-table-column>
          <el-table-column prop="ir" label="IR" width="70" align="right">
            <template #default="{ row }">{{ row.ir?.toFixed(3) || '--' }}</template>
          </el-table-column>
          <el-table-column prop="is_decaying" label="衰减" width="70" align="center">
            <template #default="{ row }"><el-tag v-if="row.is_decaying" type="danger" size="small">是</el-tag><span v-else class="text-gray">否</span></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 信号归因 -->
      <el-tab-pane label="信号归因" name="attribution">
        <div class="query-row">
          <el-input v-model="attrSignalId" placeholder="输入信号ID" style="width: 200px" @keyup.enter="queryAttribution" />
          <el-button type="primary" @click="queryAttribution" :loading="querying">查询</el-button>
        </div>
        <el-descriptions v-if="attribution" :column="2" size="small" border>
          <el-descriptions-item v-for="(val, key) in attribution" :key="key" :label="key">
            {{ typeof val === 'number' ? val.toFixed(4) : typeof val === 'object' ? JSON.stringify(val) : val }}
          </el-descriptions-item>
        </el-descriptions>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import { getSignalStats, getFactorEval, getSignalAttribution } from '@/api'
import { formatChange, changeColorClass } from '@/composables/useUtils'

const activeTab = ref('distribution')
const signalStats = ref({})
const factorEvalList = ref([])
const attrSignalId = ref('')
const querying = ref(false)
const attribution = ref(null)

const pieOption = computed(() => {
  const types = signalStats.value.type_distribution || {}
  const data = Object.entries(types).map(([name, value]) => ({ name, value }))
  if (!data.length) return { backgroundColor: 'transparent' }
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    series: [{ type: 'pie', radius: ['40%', '70%'], data, label: { color: '#1d2939' }, emphasis: { itemStyle: { shadowBlur: 10 } } }],
    color: ['#ef4444', '#f59e0b', '#3b82f6', '#22c55e', '#8b5cf6'],
  }
})

async function queryAttribution() {
  if (!attrSignalId.value) return
  querying.value = true
  try {
    const res = await getSignalAttribution(attrSignalId.value)
    attribution.value = res.attribution || null
  } catch { /* ignore */ }
  querying.value = false
}

onMounted(async () => {
  try {
    const [s, f] = await Promise.allSettled([getSignalStats(), getFactorEval()])
    if (s.status === 'fulfilled') signalStats.value = s.value.stats || {}
    if (f.status === 'fulfilled') factorEvalList.value = f.value.factors || []
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; }
@media (max-width: 768px) { .stat-row { grid-template-columns: repeat(2, 1fr); } }
</style>
