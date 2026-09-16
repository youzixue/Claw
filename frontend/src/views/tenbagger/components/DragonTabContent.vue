<template>
  <div class="table-container anomaly-table-wrap">
    <el-table :data="rows" stripe size="small" empty-text="暂无龙头数据" row-key="code" @row-click="goStock" class="anomaly-table">
      <el-table-column prop="code" label="代码" width="88" fixed>
        <template #default="{ row }"><strong>{{ row.code }}</strong></template>
      </el-table-column>
      <el-table-column prop="name" label="名称" width="92" fixed show-overflow-tooltip />
      <el-table-column prop="sector_name" label="板块/驱动" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">
          {{ row.link_role === 'follower_b' ? (row.leader_driver_reason || row.sector_name) : row.sector_name }}
        </template>
      </el-table-column>
      <el-table-column prop="level" label="角色" width="104" align="center">
        <template #default="{ row }">
          <el-tag :type="row.link_role === 'leader_a' ? 'danger' : 'warning'" size="small" effect="dark">
            {{ row.link_role === 'leader_a' ? leaderTypeLabel(row.leader_type) : 'B补涨候选' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="recognition_score" label="辨识度" width="72" align="center">
        <template #default="{ row }"><strong>{{ Number(row.recognition_score || row.score || 0).toFixed(0) }}</strong></template>
      </el-table-column>
      <el-table-column prop="tradability_score" label="可交易" width="72" align="center">
        <template #default="{ row }">
          <span :class="Number(row.tradability_score || 0) >= 60 ? 'trade-ready' : 'trade-risk'">
            {{ Number(row.tradability_score || 0).toFixed(0) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="trend_leadership_score" label="趋势力" width="72" align="center">
        <template #default="{ row }">
          <span :class="Number(row.trend_leadership_score || 0) >= 82 ? 'trade-ready' : 'muted'">
            {{ Number(row.trend_leadership_score || 0).toFixed(0) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="change_pct" label="涨跌幅" width="90" align="right">
        <template #default="{ row }"><span :class="changeColorClass(row.change_pct)">{{ formatChange(row.change_pct) }}</span></template>
      </el-table-column>
      <el-table-column prop="consecutive_days" label="连板" width="56" align="center" />
      <el-table-column label="A→B联动" min-width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.link_role === 'follower_b'">
            看{{ row.leader_name || row.leader_code }}做B · 联动{{ Number(row.linkage_score || 0).toFixed(0) }}
            · 业务{{ Number(row.business_relevance_score || 0).toFixed(0) }}
            · 归因{{ Number(row.theme_alignment_score || 0).toFixed(0) }}
            · 形态{{ Number(row.follower_shape_score || 0).toFixed(0) }}
          </span>
          <span v-else class="muted">{{ row.sector_limit_up_count || 0 }}只涨停 · {{ Math.round(Number(row.sector_up_ratio || 0) * 100) }}%上涨</span>
        </template>
      </el-table-column>
      <el-table-column prop="reasons" label="评分理由" min-width="150" show-overflow-tooltip />
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

function goStock(row) {
  router.push(`/stocks/${row.code}`)
}

function leaderTypeLabel(type) {
  return ({
    sector_leader: '板块龙头A',
    trend_leader: '趋势龙头A',
    independent_event: '独立事件A',
    emerging_leader: '独立领涨A',
  })[type] || '龙头A'
}
</script>

<style scoped lang="scss">
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

.trade-ready {
  color: var(--el-color-danger);
  font-weight: 700;
}

.trade-risk {
  color: var(--el-color-warning);
  font-weight: 600;
}

.muted {
  color: var(--claw-text-tertiary);
}
</style>
