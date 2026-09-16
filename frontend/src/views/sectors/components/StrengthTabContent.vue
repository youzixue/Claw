<template>
  <div>
    <div v-if="loading" class="loading-container">
      <el-skeleton :rows="8" animated />
    </div>
    <template v-else>
      <v-chart v-if="items.length" :option="chartOption" style="height: 400px" autoresize />
      <div class="table-container mt-16">
        <el-table :data="items" stripe size="small" empty-text="暂无板块强弱数据" class="strength-table">
          <el-table-column prop="rank" label="排名" width="60" align="center">
            <template #default="{ row }">
              <span :class="rankClass(row.rank)" class="rank-number">{{ row.rank }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="sector_name" label="板块" min-width="150">
            <template #default="{ row }">
              <div class="sector-name-cell">
                <span class="sector-name">{{ row.sector_name }}</span>
                <el-tag v-if="row.is_hot" type="danger" size="small" effect="light" class="hot-tag">热</el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="strength_score" label="强度" width="80" align="center">
            <template #default="{ row }">
              <span :class="strengthClass(row.strength_score)" class="strength-score">{{ row.strength_score }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="change_pct" label="涨跌幅" width="100" align="right">
            <template #default="{ row }">
              <span :class="changeColorClass(row.change_pct)">{{ formatChange(row.change_pct) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="fund_flow" label="板块资金净额(亿)" width="130" align="right">
            <template #default="{ row }">
              <span :class="changeColorClass(row.fund_flow)">{{ formatFundFlow(row.fund_flow) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="limit_up_count" label="涨停" width="70" align="center">
            <template #default="{ row }">
              <span :class="{ 'text-up': row.limit_up_count > 0 }">{{ row.limit_up_count || 0 }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="consecutive_days" label="连涨" width="80" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.consecutive_days >= 3" type="danger" size="small" effect="light">{{ row.consecutive_days }}天</el-tag>
              <el-tag v-else-if="row.consecutive_days >= 1" type="warning" size="small" effect="light">{{ row.consecutive_days }}天</el-tag>
              <span v-else class="text-muted">0</span>
            </template>
          </el-table-column>
          <el-table-column prop="rank_change" label="排名变化" width="90" align="center">
            <template #default="{ row }">
              <span v-if="row.rank_change > 0" class="text-up">
                <el-icon><ArrowUp /></el-icon>{{ Math.min(row.rank_change, 99) }}
              </span>
              <span v-else-if="row.rank_change < 0" class="text-down">
                <el-icon><ArrowDown /></el-icon>{{ Math.min(Math.abs(row.rank_change), 99) }}
              </span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div class="pagination-wrap" v-if="total > pageSize">
        <el-pagination
          :current-page="currentPage"
          :page-size="pageSize"
          :total="total"
          layout="prev, pager, next"
          small
          @current-change="$emit('page-change', $event)"
        />
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent } from 'vue'
import { ArrowUp, ArrowDown } from '@element-plus/icons-vue'
import { ensureBarChartsRegistered } from '@/composables/echarts/bar'
import { formatChange, changeColorClass } from '@/composables/useUtils'

ensureBarChartsRegistered()
const VChart = defineAsyncComponent(() => import('vue-echarts'))

const props = defineProps({
  loading: { type: Boolean, default: false },
  items: { type: Array, default: () => [] },
  total: { type: Number, default: 0 },
  currentPage: { type: Number, default: 1 },
  pageSize: { type: Number, default: 50 },
})

defineEmits(['page-change'])

function formatFundFlow(val) {
  if (val == null || isNaN(val)) return '--'
  const abs = Math.abs(val)
  if (abs >= 100) return val.toFixed(1) + '亿'
  if (abs >= 1) return val.toFixed(2) + '亿'
  if (abs >= 0.01) return (val * 10000).toFixed(0) + '万'
  return val.toFixed(4) + '亿'
}

function rankClass(rank) {
  if (rank <= 3) return 'text-up rank-top'
  if (rank <= 10) return 'text-warning'
  return ''
}

function strengthClass(score) {
  if (score >= 80) return 'text-up'
  if (score >= 60) return 'text-warning'
  if (score >= 40) return ''
  return 'text-down'
}

const chartOption = computed(() => {
  const items = props.items.slice(0, 15)
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const d = params[0]
        const item = items[items.length - 1 - d.dataIndex]
        if (!item) return ''
        return `<b>${item.sector_name}</b><br/>`
          + `强度: ${item.strength_score}<br/>`
          + `涨跌: ${formatChange(item.change_pct)}<br/>`
          + `板块资金: ${formatFundFlow(item.fund_flow)}<br/>`
          + `涨停: ${item.limit_up_count || 0}只<br/>`
          + `连续: ${item.consecutive_days || 0}天`
      },
    },
    grid: { left: 140, right: 50, top: 10, bottom: 20 },
    xAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: 'var(--claw-border)' } },
      axisLabel: { color: 'var(--claw-text-muted)' },
      splitLine: { lineStyle: { color: 'var(--claw-border-light)' } },
    },
    yAxis: {
      type: 'category',
      data: items.map((i) => i.sector_name).reverse(),
      axisLine: { lineStyle: { color: 'var(--claw-border)' } },
      axisLabel: { color: 'var(--claw-text-secondary)', fontSize: 11 },
    },
    series: [{
      type: 'bar',
      data: items.map((i) => ({
        value: i.strength_score,
        itemStyle: {
          color: i.strength_score >= 80 ? 'var(--claw-up)'
            : i.strength_score >= 60 ? 'var(--claw-warning)'
              : i.strength_score >= 40 ? 'var(--claw-primary)'
                : 'var(--claw-down)',
        },
      })).reverse(),
      barWidth: 14,
      label: { show: true, position: 'right', color: 'var(--claw-text-muted)', fontSize: 11 },
    }],
  }
})
</script>

<style scoped lang="scss">
.mt-16 {
  margin-top: var(--spacing-4);
}

.loading-container {
  padding: var(--spacing-10) 0;
}

.table-container {
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--claw-border);
}

.pagination-wrap {
  display: flex;
  justify-content: center;
  margin-top: var(--spacing-4);
}

:deep(.el-table) {
  border-radius: 0;
  border: none;

  th.el-table__cell {
    background: var(--neutral-50);
    font-weight: 600;
    color: var(--claw-text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
  }

  .el-table__row:hover > td {
    background: var(--primary-50);
  }
}

.sector-name-cell {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);

  .sector-name {
    font-weight: 500;
  }

  .hot-tag {
    font-size: 0.6875rem;
  }
}

.rank-number {
  font-weight: 700;
  font-size: 1rem;

  &.rank-top {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    background: var(--error-100);
    border-radius: 50%;
  }
}

.strength-score {
  font-weight: 700;
  font-size: 1rem;
}
</style>
