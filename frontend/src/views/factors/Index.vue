<template>
  <div class="page-container">
    <div class="page-shell factors-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Cpu /></el-icon>因子引擎</h2>
        <div class="page-subtitle">统一查看因子概览、评估表现、实时计算结果与报告输出</div>
      </div>
      <div class="hero-chip">
        <el-icon><Cpu /></el-icon>
        <span>因子分析工作台</span>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="因子概览" name="summary">
        <div class="metrics-panel factor-cats-panel">
          <div class="category-cards">
            <div v-for="cat in categories" :key="cat.key" class="stat-card cat-card" :class="{ active: selectedCategory === cat.key }" @click="selectedCategory = cat.key">
            <div class="metric-head"><el-icon><Grid /></el-icon><span>{{ cat.key }}</span></div>
            <div class="stat-value">{{ cat.count }}</div>
            </div>
          </div>
        </div>

        <div class="panel-card">
          <el-table :data="filteredFactors" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="name" label="因子名" min-width="150" />
            <el-table-column prop="direction" label="方向" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="row.direction === 1 ? 'danger' : row.direction === -1 ? 'success' : 'info'" size="small">{{ row.direction === 1 ? '正向' : row.direction === -1 ? '反向' : '未知' }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="描述" min-width="250" />
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="因子评估" name="evaluate">
        <div class="panel-card">
          <div class="panel-title"><el-icon><DataAnalysis /></el-icon>因子评估</div>
          <el-alert title="真实IC为因子与下一交易日收益的秩相关；旧排名自相关不再当作IC。结果仅供研究，不自动调权或晋级。" type="info" :closable="false" show-icon />
          <div class="query-row"><el-button @click="refreshEvaluation" :loading="evaluating">刷新报告</el-button><el-button type="primary" @click="runEvaluation" :loading="evaluating">运行研究评估</el-button></div>
          <el-table class="factor-eval-table" :data="evalList" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="factor_name" label="因子" min-width="150" />
            <el-table-column label="评估口径" min-width="130"><template #default="{ row }">{{ evaluationStatus(row.status) }}</template></el-table-column>
            <el-table-column prop="sample_count" label="有效日期数" width="100" />
            <el-table-column prop="ic_mean" label="IC均值" width="90" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.ic_mean)">{{ row.ic_mean?.toFixed(4) || '--' }}</span></template>
            </el-table-column>
            <el-table-column prop="ic_std" label="IC标准差" width="90" align="right">
              <template #default="{ row }">{{ row.ic_std?.toFixed(4) || '--' }}</template>
            </el-table-column>
            <el-table-column prop="ir" label="IR" width="80" align="right">
              <template #default="{ row }"><span :class="row.ir >= 0.5 ? 'text-red' : ''">{{ row.ir?.toFixed(3) || '--' }}</span></template>
            </el-table-column>
            <el-table-column prop="win_rate" label="IC正值占比" width="110" align="center">
              <template #default="{ row }">{{ formatPercent(row.win_rate) }}</template>
            </el-table-column>
            <el-table-column prop="is_decaying" label="衰减" width="70" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.is_decaying === true" type="danger" size="small">衰减</el-tag>
                <span v-else class="text-gray">{{ row.is_decaying === false ? '正常' : '待评估' }}</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="因子计算" name="compute">
        <div class="panel-card">
          <div class="panel-title"><el-icon><Search /></el-icon>因子计算</div>
          <div class="query-row">
            <el-input v-model="computeCode" placeholder="输入股票代码" style="width: 220px" @keyup.enter="doCompute" />
            <el-button type="primary" @click="doCompute" :loading="computing">计算</el-button>
          </div>
          <el-table v-if="computeResult.length" :data="computeResult" stripe size="small">
            <el-table-column prop="name" label="因子" min-width="150" />
            <el-table-column prop="value" label="值" width="120" align="right">
              <template #default="{ row }">{{ row.value?.toFixed(4) || '--' }}</template>
            </el-table-column>
            <el-table-column prop="rank" label="排名" width="80" align="center" />
            <el-table-column prop="pct" label="百分位" width="80" align="center">
              <template #default="{ row }"><span :class="Number.isFinite(row.pct) ? (row.pct >= 0.7 ? 'text-red' : row.pct <= 0.3 ? 'text-green' : '') : ''">{{ formatPercent(row.pct) }}</span></template>
            </el-table-column>
            <el-table-column prop="confidence" label="计算置信度" width="100" align="center">
              <template #default="{ row }">{{ formatPercent(row.confidence, 0) }}</template>
            </el-table-column>
            <el-table-column label="数据说明" min-width="200"><template #default="{ row }">{{ (row.meta?.input_issues || []).map(issue => `${issue.field}: ${issue.code}`).join('；') || (row.value == null ? '数据不足' : '计算有效，不代表预测概率') }}</template></el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="因子报告" name="report">
        <div class="panel-card report-panel">
          <div class="panel-title"><el-icon><Document /></el-icon>因子报告</div>
          <el-card shadow="never" v-if="report" class="inner-report-card">
            <div v-for="(val, key) in report" :key="key" class="report-row">
              <strong>{{ key }}:</strong> {{ typeof val === 'object' ? JSON.stringify(val) : val }}
            </div>
          </el-card>
          <el-empty v-else description="暂无报告" :image-size="60" />
        </div>
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getFactorCategories, evaluateFactors, computeFactors, getFactorReport, runDailyEvaluation } from '@/api'
import { ElMessage } from 'element-plus'
import { changeColorClass } from '@/composables/useUtils'

const activeTab = ref('summary')
const categories = ref([])
const selectedCategory = ref('')
const evalList = ref([])
const computeCode = ref('')
const computing = ref(false)
const computeResult = ref([])
const report = ref(null)
const evaluating = ref(false)

function formatPercent(value, digits = 1) {
  return Number.isFinite(value) ? (value * 100).toFixed(digits) + '%' : '--'
}
function evaluationStatus(status) {
  return ({ research_only: '真实收益IC·仅研究', legacy_not_ic: '旧排名自相关·非IC', insufficient_data: '样本不足' })[status] || '未评估'
}
function applyEvaluations(response) {
  evalList.value = Object.entries(response?.results || {}).map(([name, result]) => ({
    factor_name: name, status: result.status,
    ...(result.ic_summary || {}),
    win_rate: result.ic_summary?.ic_positive_rate,
    is_decaying: result.decay?.is_decaying,
  }))
}
async function refreshEvaluation() {
  evaluating.value = true
  try {
    const [evaluations, latestReport] = await Promise.all([evaluateFactors(), getFactorReport()])
    applyEvaluations(evaluations)
    report.value = latestReport
  } catch {
    ElMessage.error('评估报告读取失败，未更改数据')
  } finally {
    evaluating.value = false
  }
}
async function runEvaluation() {
  evaluating.value = true
  try {
    await runDailyEvaluation()
    await refreshEvaluation()
  } catch {
    ElMessage.error('研究评估失败，请检查数据与迁移状态')
  } finally {
    evaluating.value = false
  }
}

const filteredFactors = computed(() => {
  if (!selectedCategory.value) return categories.value.flatMap(c => c.factors || [])
  const cat = categories.value.find(c => c.key === selectedCategory.value)
  return cat?.factors || []
})

async function doCompute() {
  if (!computeCode.value) return
  computing.value = true
  try {
    const res = await computeFactors(computeCode.value)
    const factors = res.factors || {}
    computeResult.value = Object.entries(factors).map(([name, v]) => ({ name, ...v }))
  } catch {
    computeResult.value = []
    ElMessage.error('因子计算失败，请检查股票代码和数据')
  }
  computing.value = false
}

onMounted(async () => {
  try {
    const [c, e, r] = await Promise.allSettled([getFactorCategories(), evaluateFactors(), getFactorReport()])
    if (c.status === 'fulfilled') categories.value = c.value.categories || []
    if (e.status === 'fulfilled') applyEvaluations(e.value)
    else ElMessage.error('评估报告读取失败')
    if (r.status === 'fulfilled') report.value = r.value
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.factors-page { display: flex; flex-direction: column; gap: 18px; }
.factor-cats-panel { padding: 4px; margin-bottom: 16px; }
.category-cards { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 0; }
.cat-card { cursor: pointer; transition: all 0.2s; padding: 16px; background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 14px; box-shadow: var(--claw-shadow-sm); }
.cat-card:hover, .cat-card.active { border-color: var(--claw-primary); transform: translateY(-1px); }
.metric-head, .panel-title { display: inline-flex; align-items: center; gap: 8px; }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.panel-card { padding: 16px; }
.panel-title { font-size: 14px; font-weight: 600; color: var(--claw-text); margin-bottom: 14px; }
.query-row { display: flex; gap: 12px; margin-block: 12px 16px; flex-wrap: wrap; }
:deep(.factor-eval-table .el-table__cell) { padding-inline: 0; }
:deep(.factor-eval-table .cell) { white-space: nowrap; word-break: normal; padding-inline: 10px; }
.report-panel { min-height: 220px; }
.inner-report-card { border-radius: 12px; }
.report-row { margin-bottom: 10px; line-height: 1.7; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
@media (max-width: 768px) {
  .category-cards { grid-template-columns: repeat(2, 1fr); }
}
</style>
