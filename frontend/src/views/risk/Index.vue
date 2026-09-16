<template>
  <div class="page-container">
    <div class="page-shell risk-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Lock /></el-icon>风控中心</h2>
        <div class="page-subtitle">规则链、检查流程、解禁预警与情绪熔断的统一风险面板</div>
      </div>
      <div class="hero-chip">
        <el-icon><Lock /></el-icon>
        <span>交易前风险拦截</span>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="风控规则" name="rules">
        <div class="panel-card">
          <el-table :data="rules" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="name" label="规则名" width="180" />
            <el-table-column prop="description" label="描述" min-width="250" />
            <el-table-column prop="enabled" label="状态" width="100" align="center">
              <template #default="{ row }">
                <el-switch v-model="row.enabled" size="small" @change="(v) => toggleRule(row.name, v)" />
              </template>
            </el-table-column>
            <el-table-column prop="priority" label="优先级" width="80" align="center" />
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="风控检查" name="check">
        <div class="panel-card form-panel">
          <div class="panel-title"><el-icon><CircleCheck /></el-icon>手动检查</div>
          <el-form :model="checkForm" label-width="100px" size="small" class="check-form">
            <el-form-item label="股票代码"><el-input v-model="checkForm.code" /></el-form-item>
            <el-form-item label="操作">
              <el-radio-group v-model="checkForm.action"><el-radio value="buy">买入</el-radio><el-radio value="sell">卖出</el-radio></el-radio-group>
            </el-form-item>
            <el-form-item label="价格"><el-input-number v-model="checkForm.price" :min="0" :precision="2" /></el-form-item>
            <el-form-item label="数量"><el-input-number v-model="checkForm.amount" :min="0" /></el-form-item>
            <el-form-item label="总资产"><el-input-number v-model="checkForm.total_assets" :min="0" /></el-form-item>
            <el-form-item><el-button type="primary" @click="doCheck" :loading="checking">执行检查</el-button></el-form-item>
          </el-form>

          <div class="result-wrap" v-if="checkResult">
            <el-result :icon="checkResult.result === 'pass' ? 'success' : checkResult.result === 'block' ? 'error' : 'warning'"
              :title="checkResult.result === 'pass' ? '通过' : checkResult.result === 'block' ? '阻断' : '警告'">
              <template #sub-title>
                <div v-if="checkResult.reasons?.length">
                  <p v-for="r in checkResult.reasons" :key="r">{{ r }}</p>
                </div>
              </template>
            </el-result>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="解禁预警" name="lockup">
        <div class="panel-card">
          <div class="filter-row">
            <el-select v-model="riskLevelFilter" placeholder="风险等级" clearable size="small" style="width:120px">
              <el-option label="高" value="high" /><el-option label="中" value="medium" /><el-option label="低" value="low" />
            </el-select>
            <el-button size="small" @click="loadLockups">刷新</el-button>
          </div>
          <el-table :data="lockups" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="code" label="代码" width="80" />
            <el-table-column prop="name" label="名称" width="80" />
            <el-table-column prop="unlock_date" label="解禁日期" width="110" />
            <el-table-column prop="unlock_type" label="类型" width="100" />
            <el-table-column prop="unlock_volume" label="解禁量(万)" width="100" align="right">
              <template #default="{ row }">{{ row.unlock_volume ? (row.unlock_volume / 1e4).toFixed(0) : '--' }}</template>
            </el-table-column>
            <el-table-column prop="unlock_ratio" label="占比%" width="80" align="right">
              <template #default="{ row }">{{ row.unlock_ratio?.toFixed(1) || '--' }}</template>
            </el-table-column>
            <el-table-column prop="risk_level" label="风险" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="row.risk_level === 'high' ? 'danger' : row.risk_level === 'medium' ? 'warning' : 'info'" size="small">{{ row.risk_level }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="情绪熔断" name="sentiment">
        <div class="section-block sentiment-grid-wrap">
          <div class="sentiment-grid">
          <div class="panel-card" v-if="sentimentState">
            <div class="panel-title"><el-icon><Warning /></el-icon>当前状态</div>
            <div class="stat-row">
              <div class="stat-card risk-stat-card">
                <div class="stat-label">当前周期</div>
                <div class="stat-value">{{ sentimentState.cycle || '--' }}</div>
              </div>
              <div class="stat-card risk-stat-card">
                <div class="stat-label">建议仓位</div>
                <div class="stat-value text-yellow">{{ sentimentState.max_position_pct ?? '--' }}%</div>
              </div>
            </div>
            <div class="section-title"><el-icon><CloseBold /></el-icon>禁止操作</div>
            <div class="tag-row">
              <el-tag v-for="a in sentimentState.forbidden_actions || []" :key="a" type="danger" size="small">{{ a }}</el-tag>
            </div>
            <div class="section-title"><el-icon><Select /></el-icon>允许操作</div>
            <div class="tag-row">
              <el-tag v-for="a in sentimentState.allowed_actions || []" :key="a" type="success" size="small">{{ a }}</el-tag>
            </div>
          </div>

          <div class="panel-card chart-panel">
            <div class="panel-title"><el-icon><TrendCharts /></el-icon>情绪历史</div>
            <v-chart :option="sentimentHistoryOption" style="height: 280px" autoresize />
          </div>
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureLineChartsRegistered } from '@/composables/echarts/line'
import { getRiskRules, checkRisk, toggleRiskRule, getLockupUpcoming, getSentimentState, getSentimentHistory } from '@/api'

ensureLineChartsRegistered()

const activeTab = ref('rules')
const rules = ref([])
const lockups = ref([])
const riskLevelFilter = ref('')
const sentimentState = ref(null)
const sentimentHistoryList = ref([])

const checkForm = ref({ code: '', action: 'buy', price: 0, amount: 0, total_assets: 0 })
const checking = ref(false)
const checkResult = ref(null)

async function doCheck() {
  checking.value = true
  try { checkResult.value = await checkRisk(checkForm.value) } catch { /* ignore */ }
  checking.value = false
}

async function toggleRule(name, enabled) {
  try { await toggleRiskRule(name, enabled) } catch { /* ignore */ }
}

async function loadLockups() {
  try {
    const res = await getLockupUpcoming({ risk_level: riskLevelFilter.value || undefined })
    lockups.value = res.lockups || []
  } catch { /* ignore */ }
}

const sentimentHistoryOption = computed(() => {
  const items = sentimentHistoryList.value
  if (!items.length) return { backgroundColor: 'transparent' }
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.trade_date?.slice(5) || ''), axisLabel: { color: '#7a8aa0' } },
    yAxis: { type: 'value', axisLabel: { color: '#7a8aa0' }, splitLine: { lineStyle: { color: '#edf2fb' } } },
    series: [{ type: 'line', data: items.map(i => i.score), smooth: true, lineStyle: { color: '#007aff', width: 2 }, areaStyle: { color: 'rgba(0,122,255,0.12)' } }],
  }
})

onMounted(async () => {
  try {
    const [r, l, s, h] = await Promise.allSettled([getRiskRules(), getLockupUpcoming(), getSentimentState(), getSentimentHistory({ days: 30 })])
    if (r.status === 'fulfilled') rules.value = r.value.rules || []
    if (l.status === 'fulfilled') lockups.value = l.value.lockups || []
    if (s.status === 'fulfilled') sentimentState.value = s.value
    if (h.status === 'fulfilled') sentimentHistoryList.value = h.value.history || []
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.risk-page { display: flex; flex-direction: column; gap: 18px; }
.filter-row, .query-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.form-panel { max-width: 760px; }
.check-form { max-width: 600px; }
.result-wrap { margin-top: 12px; }
.section-block { display: flex; flex-direction: column; gap: 12px; }
.sentiment-grid-wrap { gap: 0; }
.sentiment-grid { display: grid; grid-template-columns: 1.1fr 1fr; gap: 16px; }
.stat-row { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; margin-bottom: 16px; }
.risk-stat-card { padding: 16px; }
.tag-row { display: flex; flex-wrap: wrap; gap: 8px; margin: 10px 0 16px; }
.chart-panel { min-height: 100%; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
@media (max-width: 768px) {
  .sentiment-grid { display: flex; flex-direction: column; align-items: stretch; }
  .stat-row { grid-template-columns: 1fr; }
}
</style>
