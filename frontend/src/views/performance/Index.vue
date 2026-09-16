<template>
  <div class="page-container">
    <div class="page-shell performance-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Trophy /></el-icon>绩效中心</h2>
        <div class="page-subtitle">统一查看信号统计、因子质量与单笔信号归因结果</div>
      </div>
      <div class="hero-chip">
        <el-icon><Trophy /></el-icon>
        <span>策略表现概览</span>
      </div>
    </div>

      <div class="metrics-panel performance-metrics-panel">
        <div class="stat-row">
          <div class="stat-card perf-stat-card">
        <div class="metric-head"><el-icon><Histogram /></el-icon><span>总信号数</span></div>
        <div class="stat-value">{{ signalStats.total || '--' }}</div>
      </div>
      <div class="stat-card perf-stat-card">
        <div class="metric-head"><el-icon><DataLine /></el-icon><span>胜率</span></div>
        <div class="stat-value" :class="signalStats.win_rate >= 0.5 ? 'text-red' : 'text-green'">{{ signalStats.win_rate ? (signalStats.win_rate * 100).toFixed(1) + '%' : '--' }}</div>
      </div>
      <div class="stat-card perf-stat-card">
        <div class="metric-head"><el-icon><TrendCharts /></el-icon><span>平均收益</span></div>
        <div class="stat-value" :class="changeColorClass(signalStats.avg_return)">{{ signalStats.avg_return ? formatChange(signalStats.avg_return) : '--' }}</div>
      </div>
          <div class="stat-card perf-stat-card">
            <div class="metric-head"><el-icon><Bottom /></el-icon><span>最大回撤</span></div>
            <div class="stat-value text-green">{{ signalStats.max_drawdown ? signalStats.max_drawdown.toFixed(2) + '%' : '--' }}</div>
          </div>
        </div>
      </div>

      <el-tabs v-model="activeTab">
      <el-tab-pane label="信号分布" name="distribution">
        <div class="panel-card chart-panel">
          <div class="panel-title"><el-icon><PieChart /></el-icon>信号类型分布</div>
          <v-chart :option="pieOption" style="height: 350px" autoresize />
        </div>
      </el-tab-pane>

      <el-tab-pane label="因子评估" name="factor-eval">
        <div class="panel-card">
          <div class="panel-title"><el-icon><Cpu /></el-icon>因子评估</div>
          <el-table class="factor-eval-table" :data="factorEvalList" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="factor_name" label="因子" min-width="150" />
            <el-table-column label="口径" min-width="130"><template #default="{ row }">{{ row.status === 'legacy_not_ic' ? '旧排名自相关·非IC' : row.status === 'research_only' ? '真实收益IC·仅研究' : '样本不足' }}</template></el-table-column>
            <el-table-column prop="ic_mean" label="IC" width="80" align="right">
              <template #default="{ row }">{{ row.ic_mean?.toFixed(4) || '--' }}</template>
            </el-table-column>
            <el-table-column prop="ir" label="IR" width="70" align="right">
              <template #default="{ row }">{{ row.ir?.toFixed(3) || '--' }}</template>
            </el-table-column>
            <el-table-column prop="is_decaying" label="衰减" width="70" align="center">
              <template #default="{ row }"><el-tag v-if="row.is_decaying === true" type="danger" size="small">是</el-tag><span v-else class="text-gray">{{ row.is_decaying === false ? '否' : '待评估' }}</span></template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="信号归因" name="attribution">
        <div class="panel-card attribution-panel">
          <div class="panel-title"><el-icon><Search /></el-icon>信号归因查询</div>
          <div class="query-row">
            <el-input v-model="attrSignalId" placeholder="输入信号ID" style="width: 240px" @keyup.enter="queryAttribution" />
            <el-button type="primary" @click="queryAttribution" :loading="querying">查询</el-button>
          </div>
          <el-descriptions v-if="attribution" :column="2" size="small" border>
            <el-descriptions-item v-for="(val, key) in attribution" :key="key" :label="key">
              {{ typeof val === 'number' ? val.toFixed(4) : typeof val === 'object' ? JSON.stringify(val) : val }}
            </el-descriptions-item>
          </el-descriptions>
        </div>
      </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensurePieChartsRegistered } from '@/composables/echarts/pie'
import { getSignalStats, getFactorEval, getSignalAttribution } from '@/api'
import { formatChange, changeColorClass } from '@/composables/useUtils'

ensurePieChartsRegistered()

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
    series: [{ type: 'pie', radius: ['40%', '70%'], data, label: { color: '#34507a' }, emphasis: { itemStyle: { shadowBlur: 10 } } }],
    color: ['#007aff', '#5ac8fa', '#34c759', '#ff9500', '#af52de'],
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
.performance-page { display: flex; flex-direction: column; gap: 18px; }
.performance-metrics-panel { padding: 4px; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 0; }
.perf-stat-card { padding: 16px; background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 14px; box-shadow: var(--claw-shadow-sm); }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.chart-panel { min-height: 390px; }
:deep(.factor-eval-table .el-table__cell) { padding-inline: 0; }
:deep(.factor-eval-table .cell) { white-space: nowrap; word-break: normal; padding-inline: 10px; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
:deep(.el-descriptions) { border-radius: 12px; overflow: hidden; }
@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, 1fr); }
}
</style>
