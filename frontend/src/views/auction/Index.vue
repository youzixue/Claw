<template>
  <div class="page-container">
    <div class="page-shell auction-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title"><el-icon class="title-icon"><Timer /></el-icon>竞价分析</h2>
          <div class="page-subtitle">统一查看竞价异动、开盘强度与个股竞价因子</div>
        </div>
        <div class="hero-chip">
          <el-icon><Timer /></el-icon>
          <span>竞价信号观察台</span>
        </div>
      </div>

      <div class="metrics-panel auction-metrics-panel">
        <div class="stat-row">
          <div class="stat-card auction-stat-card"><div class="metric-head"><span>异动总数</span></div><div class="stat-value">{{ summary.total_anomalies || '--' }}</div></div>
          <div class="stat-card auction-stat-card"><div class="metric-head"><span>高开(&gt;3%)</span></div><div class="stat-value text-red">{{ summary.high_open_count || '--' }}</div></div>
          <div class="stat-card auction-stat-card"><div class="metric-head"><span>超高开(&gt;7%)</span></div><div class="stat-value text-yellow">{{ summary.ultra_high_open_count || '--' }}</div></div>
          <div class="stat-card auction-stat-card"><div class="metric-head"><span>涨停竞价</span></div><div class="stat-value text-red">{{ summary.limit_up_bid_count || '--' }}</div></div>
          <div class="stat-card auction-stat-card"><div class="metric-head"><span>量比异动</span></div><div class="stat-value text-blue">{{ summary.volume_spike_count || '--' }}</div></div>
          <div class="stat-card auction-stat-card"><div class="metric-head"><span>低开(&lt;-3%)</span></div><div class="stat-value text-green">{{ summary.low_open_count || '--' }}</div></div>
        </div>
      </div>

      <div class="section-block">
        <div class="section-title">竞价异动信号</div>
        <div class="panel-card">
          <div class="mobile-table-wrap">
          <el-table :data="signals" stripe size="small" empty-text="暂无数据">
      <el-table-column prop="tag" label="" width="30" align="center">
        <template #default="{ row }">{{ row.tag }}</template>
      </el-table-column>
      <el-table-column prop="code" label="代码" width="80" />
      <el-table-column prop="name" label="名称" width="80" />
      <el-table-column prop="auction_price" label="竞价价" width="80" align="right" />
      <el-table-column prop="open_change" label="竞价涨幅" width="100" align="right">
        <template #default="{ row }"><span :class="changeColorClass(row.open_change)">{{ formatChange(row.open_change) }}</span></template>
      </el-table-column>
      <el-table-column prop="volume_ratio" label="量比" width="70" align="center">
        <template #default="{ row }"><span :class="row.volume_ratio >= 3 ? 'text-red' : row.volume_ratio >= 2 ? 'text-yellow' : ''">{{ row.volume_ratio?.toFixed(1) || '--' }}</span></template>
      </el-table-column>
      <el-table-column prop="auction_amount" label="竞价额" width="100" align="right">
        <template #default="{ row }">{{ formatAmount(row.auction_amount) }}</template>
      </el-table-column>
      <el-table-column prop="strength_score" label="强度" width="70" align="center">
        <template #default="{ row }">
          <el-progress :percentage="row.strength_score" :stroke-width="6" :show-text="false"
            :color="row.strength_score >= 70 ? '#ef4444' : row.strength_score >= 40 ? '#f59e0b' : '#3b82f6'" />
        </template>
      </el-table-column>
      <el-table-column label="异动类型" min-width="150">
        <template #default="{ row }">
          <el-tag v-for="a in row.anomalies" :key="a" size="small" style="margin:1px">{{ a }}</el-tag>
        </template>
      </el-table-column>
          </el-table>
          </div>
        </div>
      </div>

      <div class="section-block">
        <div class="section-title">个股竞价因子</div>
        <div class="panel-card query-panel">
          <div class="query-row">
            <el-input v-model="queryCode" placeholder="输入股票代码" style="width: 200px" @keyup.enter="queryAuctionFactors" />
            <el-button type="primary" @click="queryAuctionFactors" :loading="querying">查询</el-button>
          </div>
          <el-descriptions v-if="auctionFactors" :column="2" size="small" border>
            <el-descriptions-item v-for="(val, key) in auctionFactors" :key="key" :label="key">
              {{ typeof val === 'number' ? val.toFixed(4) : JSON.stringify(val) }}
            </el-descriptions-item>
          </el-descriptions>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getAuctionSignals, getAuctionSummary, getAuctionFactors } from '@/api'
import { formatChange, changeColorClass, formatAmount } from '@/composables/useUtils'

const signals = ref([])
const summary = ref({})
const queryCode = ref('')
const querying = ref(false)
const auctionFactors = ref(null)

async function queryAuctionFactors() {
  if (!queryCode.value) return
  querying.value = true
  try {
    const res = await getAuctionFactors(queryCode.value)
    auctionFactors.value = res.factors || null
  } catch { /* ignore */ }
  querying.value = false
}

onMounted(async () => {
  try {
    const [s, sum] = await Promise.allSettled([getAuctionSignals({ min_score: 30 }), getAuctionSummary()])
    if (s.status === 'fulfilled') signals.value = s.value.signals || []
    if (sum.status === 'fulfilled') summary.value = sum.value
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.auction-page { display: flex; flex-direction: column; gap: 18px; }
.auction-metrics-panel { padding: 4px; }
.stat-row { display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 0; }
.auction-stat-card { padding: 16px; }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.query-row { display: flex; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
:deep(.el-descriptions) { border-radius: 12px; overflow: hidden; }
@media (max-width: 768px) { .stat-row { grid-template-columns: repeat(3, 1fr); } }
</style>
