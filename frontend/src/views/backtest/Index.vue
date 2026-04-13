<template>
  <div class="page-container">
    <h2 class="page-title">⏱️ 回测引擎</h2>

    <el-tabs v-model="activeTab" >
      <!-- 信号回测 -->
      <el-tab-pane label="信号回测" name="signal">
        <el-form :model="signalForm" label-width="100px" size="small" style="max-width:600px">
          <el-form-item label="开始日期"><el-input v-model="signalForm.start_date" placeholder="YYYY-MM-DD" /></el-form-item>
          <el-form-item label="结束日期"><el-input v-model="signalForm.end_date" placeholder="YYYY-MM-DD" /></el-form-item>
          <el-form-item label="最低评分"><el-input-number v-model="signalForm.min_score" :min="0" :max="100" /></el-form-item>
          <el-form-item label="持有天数">
            <el-checkbox-group v-model="signalForm.holding_days">
              <el-checkbox :value="1">1天</el-checkbox><el-checkbox :value="3">3天</el-checkbox>
              <el-checkbox :value="5">5天</el-checkbox><el-checkbox :value="10">10天</el-checkbox>
            </el-checkbox-group>
          </el-form-item>
          <el-form-item><el-button type="primary" @click="runSignalBacktest" :loading="running">执行回测</el-button></el-form-item>
        </el-form>

        <div v-if="signalResult" class="result-section">
          <div class="stat-row">
            <div class="stat-card"><div class="stat-label">信号数</div><div class="stat-value">{{ signalResult.total || 0 }}</div></div>
            <div class="stat-card"><div class="stat-label">胜率</div><div class="stat-value">{{ signalResult.win_rate ? (signalResult.win_rate * 100).toFixed(1) + '%' : '--' }}</div></div>
          </div>
        </div>
      </el-tab-pane>

      <!-- 策略回测 -->
      <el-tab-pane label="策略回测" name="strategy">
        <el-form :model="strategyForm" label-width="100px" size="small" style="max-width:600px">
          <el-form-item label="开始日期"><el-input v-model="strategyForm.start_date" placeholder="YYYY-MM-DD" /></el-form-item>
          <el-form-item label="结束日期"><el-input v-model="strategyForm.end_date" placeholder="YYYY-MM-DD" /></el-form-item>
          <el-form-item label="初始资金"><el-input-number v-model="strategyForm.initial_capital" :min="100000" :step="100000" /></el-form-item>
          <el-form-item label="手续费%"><el-input-number v-model="strategyForm.commission_rate" :min="0" :max="0.01" :step="0.0001" :precision="4" /></el-form-item>
          <el-form-item label="止损%"><el-input-number v-model="strategyForm.stop_loss_pct" :min="1" :max="20" /></el-form-item>
          <el-form-item label="止盈%"><el-input-number v-model="strategyForm.take_profit_pct" :min="5" :max="100" /></el-form-item>
          <el-form-item label="最大持仓"><el-input-number v-model="strategyForm.max_positions" :min="1" :max="20" /></el-form-item>
          <el-form-item label="最低评分"><el-input-number v-model="strategyForm.min_score_to_buy" :min="0" :max="100" /></el-form-item>
          <el-form-item><el-button type="primary" @click="runStrategyBacktest" :loading="running">执行回测</el-button></el-form-item>
        </el-form>

        <div v-if="strategyResult" class="result-section">
          <div class="stat-row">
            <div class="stat-card"><div class="stat-label">状态</div><div class="stat-value">{{ strategyResult.status }}</div></div>
            <div class="stat-card"><div class="stat-label">初始资金</div><div class="stat-value">{{ formatAmount(strategyResult.config?.initial_capital) }}</div></div>
          </div>
        </div>
      </el-tab-pane>

      <!-- 回测结果 -->
      <el-tab-pane label="回测结果" name="results">
        <div class="query-row">
          <el-input v-model="accountId" placeholder="账户ID" style="width: 200px" @keyup.enter="loadResults" />
          <el-button type="primary" @click="loadResults" :loading="loading">查询</el-button>
        </div>

        <div v-if="perfData" class="stat-row">
          <div class="stat-card"><div class="stat-label">初始资金</div><div class="stat-value">{{ formatAmount(perfData.initial_capital) }}</div></div>
          <div class="stat-card"><div class="stat-label">当前市值</div><div class="stat-value">{{ formatAmount(perfData.current_value) }}</div></div>
          <div class="stat-card"><div class="stat-label">累计收益</div><div class="stat-value" :class="changeColorClass(perfData.total_return_pct)">{{ formatChange(perfData.total_return_pct) }}</div></div>
          <div class="stat-card"><div class="stat-label">最大回撤</div><div class="stat-value text-green">{{ perfData.max_drawdown_pct?.toFixed(2) || '--' }}%</div></div>
        </div>

        <v-chart v-if="perfNavCurve.length" :option="perfChartOption" style="height: 300px" autoresize />

        <el-table v-if="perfTrades.length" :data="perfTrades" stripe size="small" class="mt-16">
          <el-table-column prop="time" label="时间" width="160" />
          <el-table-column prop="code" label="代码" width="80" />
          <el-table-column prop="type" label="方向" width="60" align="center">
            <template #default="{ row }"><el-tag :type="row.type === 'buy' ? 'danger' : 'success'" size="small">{{ row.type === 'buy' ? '买' : '卖' }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="price" label="价格" width="80" align="right" />
          <el-table-column prop="pnl" label="盈亏" width="100" align="right">
            <template #default="{ row }"><span v-if="row.pnl" :class="changeColorClass(row.pnl)">{{ row.pnl.toFixed(2) }}</span></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import { backtestSignals, backtestStrategy, getBacktestPerformance, getBacktestTrades } from '@/api'
import { formatChange, changeColorClass, formatAmount } from '@/composables/useUtils'

const activeTab = ref('signal')
const running = ref(false)
const loading = ref(false)

const signalForm = ref({ start_date: '', end_date: '', min_score: 50, holding_days: [1, 3, 5, 10] })
const strategyForm = ref({ start_date: '', end_date: '', initial_capital: 1000000, commission_rate: 0.0003, stop_loss_pct: 7, take_profit_pct: 30, max_positions: 5, min_score_to_buy: 70 })
const signalResult = ref(null)
const strategyResult = ref(null)

const accountId = ref('')
const perfData = ref(null)
const perfNavCurve = ref([])
const perfTrades = ref([])

const perfChartOption = computed(() => {
  const items = perfNavCurve.value
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.date?.slice(5) || ''), axisLabel: { color: '#667085' } },
    yAxis: { type: 'value', axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    series: [{ type: 'line', data: items.map(i => i.nav), smooth: true, lineStyle: { color: '#06b6d4' }, areaStyle: { color: 'rgba(6,182,212,0.15)' } }],
  }
})

async function runSignalBacktest() {
  running.value = true
  try { signalResult.value = await backtestSignals(signalForm.value) } catch { /* ignore */ }
  running.value = false
}
async function runStrategyBacktest() {
  running.value = true
  try { strategyResult.value = await backtestStrategy(strategyForm.value) } catch { /* ignore */ }
  running.value = false
}
async function loadResults() {
  if (!accountId.value) return
  loading.value = true
  try {
    const [p, t] = await Promise.allSettled([getBacktestPerformance(accountId.value), getBacktestTrades(accountId.value)])
    if (p.status === 'fulfilled') { perfData.value = p.value; perfNavCurve.value = p.value.nav_curve || [] }
    if (t.status === 'fulfilled') perfTrades.value = t.value.trades || []
  } catch { /* ignore */ }
  loading.value = false
}
</script>

<style scoped lang="scss">
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 20px; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; }
.mt-16 { margin-top: 16px; }
@media (max-width: 768px) { .stat-row { grid-template-columns: repeat(2, 1fr); } }
</style>
