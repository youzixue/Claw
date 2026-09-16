<template>
  <div class="table-container anomaly-table-wrap">
    <p class="fund-research-note">共振仅供研究：板块资金为日期快照，缺少可执行来源时钟；个股过期/缺失资金不计资金分，盘后快照不代表当前交易资金或已确认收盘值。</p>
    <el-table :data="rows" stripe size="small" empty-text="暂无共振数据" row-key="code" @row-click="goStock" class="anomaly-table">
      <el-table-column prop="code" label="代码" width="88" fixed>
        <template #default="{ row }"><strong>{{ row.code }}</strong></template>
      </el-table-column>
      <el-table-column prop="name" label="名称" width="92" fixed show-overflow-tooltip />
      <el-table-column prop="stock_change" label="个股涨幅%" width="100" align="right">
        <template #default="{ row }"><span :class="changeColorClass(row.stock_change)">{{ formatChange(row.stock_change) }}</span></template>
      </el-table-column>
      <el-table-column prop="best_resonance_score" label="共振分" width="72" align="center">
        <template #default="{ row }">
          <strong :style="{ color: resonanceColor(row.best_resonance_score) }">{{ row.best_resonance_score }}</strong>
        </template>
      </el-table-column>
      <el-table-column prop="best_level" label="等级" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="resonanceTagType(row.best_level)" size="small" effect="dark">{{ resonanceLabel(row.best_level) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="个股资金证据" min-width="265">
        <template #default="{ row }">
          <div class="resonance-fund-evidence">
            <div>当前资金 {{ currentFund(row) }} <small>{{ row.main_fund_status || 'unknown' }}</small></div>
            <template v-if="row.main_fund_display?.available">
              <div>日期研究快照 {{ formatFund(row.main_fund_display.main_net_inflow) }}</div>
              <small>源时点 {{ row.main_fund_display.source_quote_at || '--' }}</small>
            </template>
            <div v-else>日期研究快照 --</div>
          </div>
        </template>
      </el-table-column>
      <el-table-column label="板块详情" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-for="(item, index) in (row.sectors || []).slice(0, 3)" :key="index" class="sector-inline">
            {{ item.sector_name }}<span :class="changeColorClass(item.sector_change)">{{ formatChange(item.sector_change) }}</span>
          </span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
import { useRouter } from 'vue-router'
import { changeColorClass, formatChange } from '@/composables/useUtils'

defineProps({
  rows: { type: Array, default: () => [] },
})

const router = useRouter()

function formatFund(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '--'
  return `${value > 0 ? '+' : ''}${(value / 1e8).toFixed(2)}亿`
}

function currentFund(row) {
  return row.main_fund_status === 'ok' ? formatFund(row.main_net_inflow) : '--'
}

function goStock(row) {
  router.push(`/stocks/${row.code}`)
}

function resonanceColor(score) {
  if (score >= 80) return '#ef4444'
  if (score >= 60) return '#f97316'
  if (score >= 40) return '#409eff'
  return '#98a2b3'
}

function resonanceLabel(level) {
  return ({
    strong_resonance: '强共振',
    weak_resonance: '弱共振',
    independent: '独立行情',
    counter_trend: '逆势',
  })[level] || level
}

function resonanceTagType(level) {
  return ({
    strong_resonance: 'danger',
    weak_resonance: 'warning',
    independent: 'info',
    counter_trend: 'info',
  })[level] || 'info'
}
</script>

<style scoped lang="scss">
.fund-research-note {
  padding: 0 12px;
  color: var(--claw-text-secondary);
  font-size: 12px;
}

.sector-inline {
  margin-right: 8px;
}

.table-container,
.anomaly-table-wrap {
  margin-top: var(--spacing-4);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

:deep(.el-table) {
  border: none;
  cursor: pointer;

  th.el-table__cell {
    background: var(--neutral-50);
    font-weight: 600;
    color: var(--claw-text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
    padding: 10px 0;
  }

  td.el-table__cell {
    padding: 9px 0;
  }

  .el-table__row:hover > td {
    background: var(--primary-50);
  }
}
</style>
