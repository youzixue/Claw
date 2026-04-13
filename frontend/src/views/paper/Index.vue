<template>
  <div class="page-container">
    <h2 class="page-title">💼 模拟盘</h2>

    <el-tabs v-model="activeTab" >
      <!-- 账户概览 -->
      <el-tab-pane label="账户概览" name="account">
        <div class="stat-row">
          <div class="stat-card"><div class="stat-label">总资产</div><div class="stat-value">{{ formatAmount(account.total_assets) }}</div></div>
          <div class="stat-card"><div class="stat-label">累计收益</div><div class="stat-value" :class="changeColorClass(account.total_return)">{{ formatChange(account.total_return) }}</div></div>
          <div class="stat-card"><div class="stat-label">最大回撤</div><div class="stat-value text-green">{{ account.max_drawdown?.toFixed(2) || '--' }}%</div></div>
          <div class="stat-card"><div class="stat-label">夏普比率</div><div class="stat-value">{{ account.sharpe_ratio?.toFixed(2) || '--' }}</div></div>
          <div class="stat-card"><div class="stat-label">胜率</div><div class="stat-value">{{ account.win_rate ? (account.win_rate * 100).toFixed(1) + '%' : '--' }}</div></div>
        </div>
        <div class="section-title">净值曲线</div>
        <v-chart :option="navChartOption" style="height: 300px" autoresize />
      </el-tab-pane>

      <!-- 当前持仓 -->
      <el-tab-pane label="当前持仓" name="positions">
        <el-table :data="positions" stripe size="small" empty-text="暂无持仓">
          <el-table-column prop="code" label="代码" width="80">
            <template #default="{ row }"><router-link :to="`/stocks/${row.code}`" class="link">{{ row.code }}</router-link></template>
          </el-table-column>
          <el-table-column prop="name" label="名称" width="80" />
          <el-table-column prop="buy_price" label="买入价" width="80" align="right" />
          <el-table-column prop="current_price" label="现价" width="80" align="right" />
          <el-table-column prop="profit_pct" label="盈亏%" width="90" align="right">
            <template #default="{ row }"><span :class="changeColorClass(row.profit_pct)">{{ row.profit_pct?.toFixed(2) || '--' }}%</span></template>
          </el-table-column>
          <el-table-column prop="hold_days" label="持天数" width="70" align="center" />
          <el-table-column prop="stop_loss_price" label="止损价" width="80" align="right" />
          <el-table-column prop="buy_reason" label="原因" min-width="150" show-overflow-tooltip />
        </el-table>
      </el-tab-pane>

      <!-- 交易操作 -->
      <el-tab-pane label="交易操作" name="trade">
        <div class="trade-forms">
          <el-card shadow="never" class="trade-card">
            <template #header><span class="text-red">买入</span></template>
            <el-form :model="buyForm" label-width="80px" size="small">
              <el-form-item label="代码"><el-input v-model="buyForm.code" /></el-form-item>
              <el-form-item label="价格"><el-input-number v-model="buyForm.price" :min="0" :precision="2" /></el-form-item>
              <el-form-item label="数量"><el-input-number v-model="buyForm.amount" :min="100" :step="100" /></el-form-item>
              <el-form-item label="信号ID"><el-input v-model="buyForm.signal_id" /></el-form-item>
              <el-form-item><el-button type="danger" @click="doBuy">确认买入</el-button></el-form-item>
            </el-form>
          </el-card>
          <el-card shadow="never" class="trade-card">
            <template #header><span class="text-green">卖出</span></template>
            <el-form :model="sellForm" label-width="80px" size="small">
              <el-form-item label="代码"><el-input v-model="sellForm.code" /></el-form-item>
              <el-form-item label="价格"><el-input-number v-model="sellForm.price" :min="0" :precision="2" /></el-form-item>
              <el-form-item label="数量"><el-input-number v-model="sellForm.amount" :min="100" :step="100" /></el-form-item>
              <el-form-item label="原因"><el-input v-model="sellForm.reason" /></el-form-item>
              <el-form-item><el-button type="success" @click="doSell">确认卖出</el-button></el-form-item>
            </el-form>
          </el-card>
        </div>
      </el-tab-pane>

      <!-- 交易记录 -->
      <el-tab-pane label="交易记录" name="trades">
        <el-table :data="trades" stripe size="small" empty-text="暂无记录">
          <el-table-column prop="trade_time" label="时间" width="160" />
          <el-table-column prop="code" label="代码" width="80" />
          <el-table-column prop="type" label="方向" width="60" align="center">
            <template #default="{ row }"><el-tag :type="row.type === 'buy' ? 'danger' : 'success'" size="small">{{ row.type === 'buy' ? '买' : '卖' }}</el-tag></template>
          </el-table-column>
          <el-table-column prop="price" label="价格" width="80" align="right" />
          <el-table-column prop="shares" label="数量" width="80" align="right" />
          <el-table-column prop="pnl" label="盈亏" width="100" align="right">
            <template #default="{ row }"><span v-if="row.pnl" :class="changeColorClass(row.pnl)">{{ row.pnl.toFixed(2) }}</span><span v-else>--</span></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted } from 'vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import { getPaperAccount, getPaperPositions, paperBuy, paperSell, getPaperNav, getPaperTrades } from '@/api'
import { formatChange, changeColorClass, formatAmount } from '@/composables/useUtils'
import { ElMessage } from 'element-plus'

const activeTab = ref('account')
const account = ref({})
const positions = ref([])
const navList = ref([])
const trades = ref([])

const buyForm = ref({ code: '', price: 0, amount: 100, signal_id: '' })
const sellForm = ref({ code: '', price: 0, amount: 100, reason: '' })

const navChartOption = computed(() => {
  const items = navList.value
  if (!items.length) return { backgroundColor: 'transparent' }
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    grid: { left: 60, right: 20, top: 10, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.date?.slice(5) || ''), axisLabel: { color: '#667085' } },
    yAxis: { type: 'value', axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    series: [{ type: 'line', data: items.map(i => i.nav), smooth: true, lineStyle: { color: '#06b6d4' }, areaStyle: { color: 'rgba(6,182,212,0.15)' } }],
  }
})

async function doBuy() {
  try { await paperBuy(buyForm.value); ElMessage.success('买入成功'); await loadData() } catch { /* ignore */ }
}
async function doSell() {
  try { await paperSell(sellForm.value); ElMessage.success('卖出成功'); await loadData() } catch { /* ignore */ }
}

async function loadData() {
  try {
    const [a, p, n, t] = await Promise.allSettled([getPaperAccount(), getPaperPositions(), getPaperNav(), getPaperTrades()])
    if (a.status === 'fulfilled') account.value = a.value.account || {}
    if (p.status === 'fulfilled') positions.value = p.value.positions || []
    if (n.status === 'fulfilled') navList.value = n.value.nav || []
    if (t.status === 'fulfilled') trades.value = t.value.trades || []
  } catch { /* ignore */ }
}

onMounted(loadData)
</script>

<style scoped lang="scss">
.stat-row { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-bottom: 20px; }
.trade-forms { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.trade-card { border-radius: 8px; }
.link { color: var(--claw-primary); text-decoration: none; }
@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, 1fr); }
  .trade-forms { grid-template-columns: 1fr; }
}
</style>
