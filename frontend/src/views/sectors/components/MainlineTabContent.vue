<template>
  <div>
    <div v-if="loading" class="loading-container">
      <el-skeleton :rows="5" animated />
    </div>
    <template v-else>
      <div class="mainline-section">
        <h3 class="section-title">
          <el-icon :size="18" class="title-icon"><TrendCharts /></el-icon>
          当前主线 ({{ activeCount || 0 }})
          <el-tag type="danger" size="small" effect="light">四档口径</el-tag>
        </h3>

        <div class="mainline-grid" v-if="activeMainLines.length">
          <div
            v-for="ml in activeMainLines"
            :key="ml.sector_code"
            class="mainline-card"
            :class="'mainline-card-' + (ml.main_line_status || 'none')"
          >
            <div class="card-header">
              <div class="sector-info">
                <span class="sector-name">{{ ml.sector_name }}</span>
                <el-tag :type="ml.sector_type === 'concept' ? 'danger' : 'warning'" size="small" effect="light">
                  {{ ml.sector_type === 'concept' ? '概念' : '行业' }}
                </el-tag>
                <el-tag
                  v-if="ml.main_line_status && ml.main_line_status !== 'none'"
                  :type="mainLineTagType(ml.main_line_status)"
                  size="small"
                  effect="light"
                  class="mainline-status-chip"
                >
                  {{ ml.main_line_label }}
                </el-tag>
              </div>
              <div class="duration-badge">
                <el-icon><Timer /></el-icon>
                {{ ml.duration_days }}天
              </div>
            </div>
            <div class="card-stats">
              <div class="stat-item">
                <span class="stat-label">最高板</span>
                <span class="stat-value text-up">{{ ml.max_height }}板</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">总涨停</span>
                <span class="stat-value">{{ ml.total_limit_up }}只</span>
              </div>
              <div class="stat-item">
                <span class="stat-label">龙头股</span>
                <span class="stat-value leader-name" v-if="ml.leader_stock">
                  {{ ml.leader_name }}
                  <el-tag type="danger" size="small" effect="light">{{ ml.leader_max_height }}板</el-tag>
                </span>
                <span v-else class="stat-value text-muted">-</span>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-else description="暂无活跃主线" />

        <h3 class="section-title mt-24" v-if="endedMainLines.length">
          <el-icon :size="18" class="title-icon"><Warning /></el-icon>
          近期结束主线
          <el-tag type="info" size="small" effect="light">回避</el-tag>
        </h3>
        <div class="table-container" v-if="endedMainLines.length">
          <el-table
            :data="endedMainLines"
            stripe
            size="small"
          >
            <el-table-column prop="sector_name" label="板块" min-width="140" />
            <el-table-column prop="duration_days" label="持续天数" width="100" align="center" />
            <el-table-column prop="max_height" label="最高板" width="90" align="center" />
            <el-table-column prop="end_reason" label="结束原因" min-width="180" />
          </el-table>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { TrendCharts, Timer, Warning } from '@element-plus/icons-vue'

defineProps({
  loading: { type: Boolean, default: false },
  activeCount: { type: Number, default: 0 },
  activeMainLines: { type: Array, default: () => [] },
  endedMainLines: { type: Array, default: () => [] },
  mainLineTagType: { type: Function, required: true },
})
</script>

<style scoped lang="scss">
.loading-container {
  padding: var(--spacing-10) 0;
}

.mt-24 {
  margin-top: var(--spacing-6);
}

.table-container {
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--claw-border);
}

.mainline-section {
  .section-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-2);
    font-size: 1.125rem;
    font-weight: 600;
    color: var(--claw-text-primary);
    margin-bottom: var(--spacing-4);
  }

  .title-icon {
    color: var(--claw-primary);
  }
}

.mainline-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-4);
  margin-bottom: var(--spacing-6);

  @media (max-width: 1919px) {
    grid-template-columns: repeat(2, 1fr);
  }

  @media (max-width: 1023px) {
    grid-template-columns: 1fr;
  }
}

.mainline-card {
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-5);
  box-shadow: var(--shadow-sm);
  transition: all var(--transition-base);

  &:hover {
    box-shadow: var(--shadow-md);
    transform: translateY(-2px);
  }

  &.mainline-card-strengthening {
    border-color: rgba(239, 68, 68, 0.28);
    box-shadow: 0 10px 24px rgba(239, 68, 68, 0.10);
    background: linear-gradient(180deg, rgba(255, 247, 247, 0.98) 0%, var(--claw-bg-card) 100%);
  }

  &.mainline-card-continuing {
    border-color: rgba(245, 158, 11, 0.22);
    box-shadow: 0 8px 18px rgba(245, 158, 11, 0.08);
    background: linear-gradient(180deg, rgba(255, 251, 235, 0.95) 0%, var(--claw-bg-card) 100%);
  }

  &.mainline-card-diverging {
    border-color: rgba(148, 163, 184, 0.22);
    box-shadow: none;
    background: linear-gradient(180deg, rgba(248, 250, 252, 0.92) 0%, var(--claw-bg-card) 100%);
    opacity: 0.88;
  }
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: var(--spacing-4);
}

.sector-info {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
}

.sector-name {
  font-size: 1.125rem;
  font-weight: 600;
  color: var(--claw-text-primary);
}

.duration-badge {
  display: flex;
  align-items: center;
  gap: var(--spacing-1);
  padding: var(--spacing-1) var(--spacing-2);
  background: var(--primary-50);
  color: var(--primary-700);
  border-radius: var(--radius-sm);
  font-size: 0.8125rem;
  font-weight: 500;
}

.card-stats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-3);
}

.stat-item {
  text-align: center;
  padding: var(--spacing-3);
  background: var(--neutral-50);
  border-radius: var(--radius-md);
}

.stat-label {
  font-size: 0.75rem;
  color: var(--claw-text-muted);
  margin-bottom: var(--spacing-1);
}

.stat-value {
  font-size: 1.125rem;
  font-weight: 700;
  font-variant-numeric: tabular-nums;

  &.leader-name {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: var(--spacing-1);
    font-size: 0.875rem;
  }
}

.mainline-card-diverging {
  .duration-badge {
    background: var(--neutral-100);
    color: var(--neutral-600);
  }

  .stat-item {
    background: rgba(148, 163, 184, 0.06) !important;
  }

  .sector-name,
  .stat-value {
    color: var(--claw-text-secondary) !important;
  }
}
</style>
