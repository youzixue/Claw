<template>
  <div>
    <div v-if="loading || lifecycleLoading" class="loading-container">
      <el-skeleton :rows="8" animated />
    </div>
    <template v-else>
      <div class="state-filters mb-16">
        <el-radio-group :model-value="stateFilter" size="small" @change="handleFilterChange">
          <el-radio-button value="">全部</el-radio-button>
          <el-radio-button value="emerging">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-emerging"><component :is="stateIcon('emerging')" /></el-icon>
              刚启动
            </span>
          </el-radio-button>
          <el-radio-button value="accelerating">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-accelerating"><component :is="stateIcon('accelerating')" /></el-icon>
              加速
            </span>
          </el-radio-button>
          <el-radio-button value="climax">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-climax"><component :is="stateIcon('climax')" /></el-icon>
              高潮
            </span>
          </el-radio-button>
          <el-radio-button value="diverging">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-diverging"><component :is="stateIcon('diverging')" /></el-icon>
              分化
            </span>
          </el-radio-button>
          <el-radio-button value="declining">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-declining"><component :is="stateIcon('declining')" /></el-icon>
              退潮
            </span>
          </el-radio-button>
          <el-radio-button value="one_day">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-one-day"><component :is="stateIcon('one_day')" /></el-icon>
              一日游
            </span>
          </el-radio-button>
          <el-radio-button value="capital_probe">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-capital-probe"><component :is="stateIcon('capital_probe')" /></el-icon>
              资金试探
            </span>
          </el-radio-button>
          <el-radio-button value="dormant">
            <span class="state-pill-label">
              <el-icon class="state-pill-icon icon-dormant"><component :is="stateIcon('dormant')" /></el-icon>
              休眠
            </span>
          </el-radio-button>
        </el-radio-group>
      </div>

      <div class="lc-summary-bar mb-16" v-if="lifecycleStats">
        <el-tooltip
          v-for="(stat, state) in lifecycleStats"
          :key="state"
          placement="top"
          :show-after="120"
        >
          <template #content>
            <div style="max-width:240px;line-height:1.6;font-size:13px">{{ stateDesc(state) }}</div>
          </template>
          <span
            class="lc-stat-item"
            @click="handleFilterChange(state)"
            :class="{ active: stateFilter === state }"
          >
            <el-icon class="lc-state-icon" :class="'lc-state-icon-' + state">
              <component :is="stateIcon(state)" />
            </el-icon>
            <span class="lc-name">{{ stateLabel(state) }}</span>
            <b>{{ stat.count }}</b>
            <i class="lc-info" title="查看状态说明">?</i>
          </span>
        </el-tooltip>
      </div>

      <div class="table-container">
        <el-table :data="lifecycleList" stripe size="small" empty-text="暂无生命周期数据" class="lifecycle-table">
          <el-table-column prop="sector_name" label="板块" min-width="170">
            <template #default="{ row }">
              <div class="sector-name-cell">
                <span class="sector-name">{{ row.sector_name }}</span>
                <el-tag
                  v-if="row.main_line_status && row.main_line_status !== 'none'"
                  :type="mainLineTagType(row.main_line_status)"
                  size="small"
                  effect="light"
                  class="main-line-tag"
                >
                  {{ row.main_line_label }}
                </el-tag>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="state_label" label="状态" width="120" align="center">
            <template #default="{ row }">
              <el-tooltip placement="top" :show-after="120">
                <template #content>
                  <div class="state-reason-tooltip">
                    <div class="state-reason-title">{{ displayStateLabel(row) }}判定依据</div>
                    <div class="state-reason-line">涨停/首板/连板: {{ displayLimitUpCount(row) }} / {{ displayFirstBoardCount(row) }} / {{ displayConsecutiveBoardCount(row) }}</div>
                    <div class="state-reason-line">最高板: {{ displayMaxBoardHeight(row) > 0 ? (displayMaxBoardHeight(row) >= 2 ? displayMaxBoardHeight(row) + '连板' : '首板') : '-' }}</div>
                    <div class="state-reason-line">板块资金净额: {{ formatFundFlow(row.fund_flow) }}</div>
                    <div class="state-reason-line">强度分: {{ row.strength_score != null ? Math.round(row.strength_score) : (row.state_score ?? '-') }}</div>
                    <div class="state-reason-line">K线信号: {{ klineJudgeHint(row) }}</div>
                    <div v-if="isCapitalProbe(row)" class="state-reason-line text-warning">辅助信号: 资金试探，说明资金与趋势先启动，但涨停结构尚未成型。</div>
                    <div v-if="row.attribution_confidence_label && row.attribution_confidence !== 'none'" class="state-reason-line">归因置信度: {{ row.attribution_confidence_label }}</div>
                    <div v-if="row.attribution_confidence && row.attribution_confidence !== 'none'" class="state-reason-line text-muted">
                      {{ attributionConfidenceHint(row.attribution_confidence) }}
                    </div>
                    <div v-if="row.attributed_reason_samples?.length" class="state-reason-line">归因原因: {{ row.attributed_reason_samples.join('、') }}</div>
                    <div v-if="hasLifecycleRawFallback(row)" class="state-reason-line text-warning">映射参考(不参与状态判断): {{ row.raw_limit_up_count || 0 }} / {{ row.raw_first_board_count || 0 }} / {{ row.raw_consecutive_board_count || 0 }}，最高板 {{ row.raw_max_board_height > 0 ? (row.raw_max_board_height >= 2 ? row.raw_max_board_height + '连板' : '首板') : '-' }}</div>
                    <div v-if="hasLifecycleRawFallback(row) && row.raw_reason_samples?.length" class="state-reason-line text-warning">映射参考原因: {{ row.raw_reason_samples.join('、') }}</div>
                    <div class="state-reason-line">主生命周期: {{ stateLabel(row.lifecycle_state) }}</div>
                    <div class="state-reason-line">{{ stateJudgeHint(row) }}</div>
                  </div>
                </template>
                <el-tag :type="displayStateTagType(row)" size="small" effect="light" round class="lifecycle-state-tag">
                  <span class="state-tag-content">
                    <el-icon class="state-tag-icon" :class="'state-tag-icon-' + displayStateKey(row)">
                      <component :is="stateIcon(displayStateKey(row))" />
                    </el-icon>
                    {{ displayStateLabel(row) }}
                  </span>
                </el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column prop="change_pct" label="涨跌" width="110" align="right">
            <template #default="{ row }">
              <span v-if="row.change_pct != null && row.change_pct != 0" :class="changeColorClass(row.change_pct)">
                {{ formatChange(row.change_pct) }}
              </span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="state_score" label="强度" width="80" align="center">
            <template #default="{ row }">
              <span :class="strengthClass(row.strength_score ?? row.state_score)">
                {{ row.strength_score != null ? Math.round(row.strength_score) : (row.state_score ?? '-') }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="涨停/首板/连板" width="170" align="center">
            <template #default="{ row }">
              <el-popover placement="right" :width="400" trigger="hover" v-if="displayLadderStocks(row)?.length">
                <template #reference>
                  <span class="cursor-pointer limit-up-stats">
                    <span class="text-up">{{ displayLimitUpCount(row) ?? '-' }}</span>
                    <span class="separator">/</span>
                    <span>{{ displayFirstBoardCount(row) ?? '-' }}</span>
                    <span class="separator">/</span>
                    <span class="text-warning">{{ displayConsecutiveBoardCount(row) ?? '-' }}</span>
                  </span>
                </template>
                <div class="ladder-popover">
                  <div class="ladder-header">
                    连板梯队 (涨停{{ displayLimitUpCount(row) }}/首板{{ displayFirstBoardCount(row) }}/连板{{ displayConsecutiveBoardCount(row) }})
                  </div>
                  <div v-for="ladder in displayLadderStocks(row)" :key="ladder.height" class="ladder-row">
                    <span class="ladder-height" :class="heightClass(ladder.height)">
                      {{ ladder.height >= 2 ? ladder.height + '连板' : '首板' }}
                    </span>
                    <span class="ladder-stocks">
                      <el-tag
                        v-for="s in ladder.stocks.slice(0, 8)"
                        :key="s.code"
                        size="small"
                        :type="ladder.height >= 3 ? 'danger' : 'warning'"
                        class="stock-tag cursor-pointer"
                        @click="goStock(s.code)"
                      >
                        {{ s.name }}
                      </el-tag>
                      <span v-if="ladder.stocks.length > 8" class="text-muted">+{{ ladder.stocks.length - 8 }}</span>
                    </span>
                  </div>
                  <div v-if="hasLifecycleRawFallback(row)" class="ladder-note">
                    原始映射参考: {{ row.raw_limit_up_count || 0 }}/{{ row.raw_first_board_count || 0 }}/{{ row.raw_consecutive_board_count || 0 }}
                    <span v-if="row.raw_reason_samples?.length">；原因: {{ row.raw_reason_samples.join('、') }}</span>
                  </div>
                </div>
              </el-popover>
              <template v-else>
                <span class="limit-up-stats">
                  <span class="text-up">{{ displayLimitUpCount(row) ?? '-' }}</span>
                  <span class="separator">/</span>
                  <span>{{ displayFirstBoardCount(row) ?? '-' }}</span>
                  <span class="separator">/</span>
                  <span class="text-warning">{{ displayConsecutiveBoardCount(row) ?? '-' }}</span>
                </span>
              </template>
            </template>
          </el-table-column>
          <el-table-column prop="max_board_height" label="最高板" width="110" align="center">
            <template #default="{ row }">
              <template v-if="displayMaxBoardHeight(row) > 0 && displayLeaderStocks(row)?.length">
                <span :class="heightClass(displayMaxBoardHeight(row))" class="cursor-pointer board-height" @click="goStock(displayLeaderStocks(row)[0].code)">
                  {{ displayMaxBoardHeight(row) >= 2 ? displayMaxBoardHeight(row) + '连板' : '首板' }}
                </span>
              </template>
              <span v-else-if="displayMaxBoardHeight(row) > 0" :class="heightClass(displayMaxBoardHeight(row))" class="board-height">
                {{ displayMaxBoardHeight(row) >= 2 ? displayMaxBoardHeight(row) + '连板' : '首板' }}
              </span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column label="龙头股" width="220" align="center">
            <template #default="{ row }">
              <template v-if="displayLeaderStocks(row) && displayLeaderStocks(row).length > 0">
                <el-tag size="default" type="danger" effect="light" class="leader-tag cursor-pointer" @click="goStock(displayLeaderStocks(row)[0].code)">
                  {{ displayLeaderStocks(row)[0].name }}
                  <span class="leader-height">({{ displayLeaderStocks(row)[0].height >= 2 ? displayLeaderStocks(row)[0].height + '连板' : '首板' }})</span>
                </el-tag>
              </template>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="fund_flow" label="板块资金净额(亿)" width="150" align="right">
            <template #default="{ row }">
              <span v-if="row.fund_flow != null && row.fund_flow !== ''" :class="changeColorClass(row.fund_flow)">
                {{ formatFundFlow(row.fund_flow) }}
              </span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
          <el-table-column prop="quality_score" label="质量分" width="90" align="center">
            <template #default="{ row }">
              <span v-if="row.quality_score != null && row.quality_score > 0" :class="qualityClass(row.quality_score)">
                {{ Math.round(row.quality_score) }}
              </span>
              <span v-else class="text-muted">-</span>
            </template>
          </el-table-column>
        </el-table>
      </div>

      <div class="pagination-wrap" v-if="lifecycleTotal > pageSize">
        <div class="pagination-total">共 {{ lifecycleTotal }} 个{{ categoryLabel }}</div>
        <el-pagination
          :current-page="currentPage"
          :page-size="pageSize"
          :total="lifecycleTotal"
          layout="prev, pager, next"
          small
          @current-change="emit('change-page', $event)"
        />
      </div>
    </template>
  </div>
</template>

<script setup>
const props = defineProps({
  loading: { type: Boolean, default: false },
  lifecycleLoading: { type: Boolean, default: false },
  lifecycleStats: { type: Object, default: null },
  lifecycleList: { type: Array, default: () => [] },
  lifecycleTotal: { type: Number, default: 0 },
  pageSize: { type: Number, default: 50 },
  currentPage: { type: Number, default: 1 },
  stateFilter: { type: String, default: '' },
  categoryLabel: { type: String, default: '' },
  stateDesc: { type: Function, required: true },
  stateIcon: { type: Function, required: true },
  stateLabel: { type: Function, required: true },
  displayStateLabel: { type: Function, required: true },
  displayLimitUpCount: { type: Function, required: true },
  displayFirstBoardCount: { type: Function, required: true },
  displayConsecutiveBoardCount: { type: Function, required: true },
  displayMaxBoardHeight: { type: Function, required: true },
  formatFundFlow: { type: Function, required: true },
  displayStateTagType: { type: Function, required: true },
  displayStateKey: { type: Function, required: true },
  displayLadderStocks: { type: Function, required: true },
  displayLeaderStocks: { type: Function, required: true },
  heightClass: { type: Function, required: true },
  goStock: { type: Function, required: true },
  isCapitalProbe: { type: Function, required: true },
  hasLifecycleRawFallback: { type: Function, required: true },
  attributionConfidenceHint: { type: Function, required: true },
  stateJudgeHint: { type: Function, required: true },
  klineJudgeHint: { type: Function, required: true },
  changeColorClass: { type: Function, required: true },
  strengthClass: { type: Function, required: true },
  qualityClass: { type: Function, required: true },
  mainLineTagType: { type: Function, required: true },
  formatChange: { type: Function, required: true },
})

const emit = defineEmits(['change-filter', 'change-page'])

function handleFilterChange(value) {
  emit('change-filter', value)
}
</script>

<style scoped lang="scss">
.mb-16 {
  margin-bottom: var(--spacing-4);
}

.loading-container {
  padding: var(--spacing-10) 0;
}

.pagination-total {
  font-size: 0.8125rem;
  color: var(--claw-text-muted);
}

.state-reason-tooltip {
  max-width: 280px;
  line-height: 1.6;
  font-size: 12px;
}

.state-reason-title {
  font-size: 13px;
  font-weight: 700;
  margin-bottom: 4px;
}

.state-reason-line {
  color: rgba(255, 255, 255, 0.92);
}

.table-container {
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--claw-border);
}

.state-filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-1);
  margin-bottom: var(--spacing-3);
  padding-left: 2px;
  overflow: visible;

  :deep(.el-radio-group) {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-2);
    padding-left: 1px;
    overflow: visible;
  }

  :deep(.el-radio-button) {
    margin: 0 !important;
  }

  :deep(.el-radio-button__inner) {
    height: 27px;
    line-height: 25px;
    padding: 0 9px;
    font-size: 0.8125rem;
    font-weight: 500;
    border-radius: var(--radius-md);
    border: 0 !important;
    margin-left: 0 !important;
    box-shadow: none !important;
    color: var(--claw-text-secondary);
    background: rgba(255, 255, 255, 0.72);
  }

  :deep(.el-radio-button.is-active .el-radio-button__inner),
  :deep(.el-radio-button.is-active .el-radio-button__original-radio:not(:disabled)+.el-radio-button__inner) {
    box-shadow: none !important;
    color: #fff !important;
    background: linear-gradient(135deg, #5ea2f6 0%, #4f8fe6 100%) !important;
  }

  .state-pill-label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .state-pill-icon {
    font-size: 13px;
    flex-shrink: 0;
  }

  .icon-emerging { color: #59c36a; }
  .icon-accelerating { color: #4f9df0; }
  .icon-climax { color: #e15248; }
  .icon-diverging { color: #f2bf3a; }
  .icon-declining { color: #8b97ac; }
  .icon-one-day { color: #c6ccd6; }
}

.lc-summary-bar {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-1);
  align-items: center;
  padding: 4px 8px;
  margin-top: 4px;
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: var(--radius-lg);
}

.lc-stat-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 8px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-size: 0.65625rem;
  user-select: none;
  white-space: nowrap;
  background: var(--claw-bg-card);
  border: 1px solid rgba(148, 163, 184, 0.22);

  &:hover,
  &.active {
    transform: translateY(-0.5px);
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.07);
    border-color: var(--claw-primary);
  }

  &.active {
    font-weight: 600;
    background: var(--primary-50);
  }

  .lc-state-icon {
    font-size: 12px;
    display: inline-flex;
    flex-shrink: 0;
  }

  .lc-name {
    color: var(--claw-text-secondary);
  }

  b {
    color: var(--claw-text-primary);
    font-size: 0.75rem;
    min-width: 12px;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }
}

.lc-info {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: rgba(59, 130, 246, 0.12);
  color: var(--claw-primary);
  border: 1px solid rgba(59, 130, 246, 0.24);
  font-size: 8px;
  font-weight: 700;
  font-style: normal;
  margin-left: 1px;
}

.lc-state-icon-emerging { color: var(--success-500); }
.lc-state-icon-accelerating { color: var(--claw-primary); }
.lc-state-icon-climax { color: var(--error-500); }
.lc-state-icon-diverging { color: var(--claw-warning); }
.lc-state-icon-declining { color: var(--neutral-500); }
.lc-state-icon-one_day { color: var(--neutral-400); }
.lc-state-icon-dormant { color: var(--neutral-300); }

.lifecycle-state-tag {
  :deep(.el-tag__content) {
    display: inline-flex;
    align-items: center;
  }
}

.state-tag-content {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.state-tag-icon {
  font-size: 12px;
}

.state-tag-icon-emerging { color: var(--success-500); }
.state-tag-icon-accelerating { color: var(--claw-primary); }
.state-tag-icon-climax { color: var(--error-500); }
.state-tag-icon-diverging { color: var(--claw-warning); }
.state-tag-icon-declining { color: var(--neutral-500); }
.state-tag-icon-one_day { color: var(--neutral-400); }
.state-tag-icon-dormant { color: var(--neutral-300); }

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

.lifecycle-table {
  :deep(.el-table__cell .cell) {
    white-space: nowrap;
  }
}

.sector-name-cell {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);

  .sector-name {
    font-weight: 500;
  }

  .main-line-tag {
    font-size: 0.6875rem;
  }
}

.limit-up-stats {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-1);
  font-weight: 500;

  .separator {
    color: var(--claw-text-muted);
  }
}

.board-height {
  font-weight: 600;
  padding: var(--spacing-1) var(--spacing-2);
  border-radius: var(--radius-sm);
  background: var(--neutral-50);
}

.leader-tag {
  font-weight: 600;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;

  &:hover {
    transform: scale(1.05);
  }

  .leader-height {
    font-weight: 400;
    opacity: 0.8;
  }
}

.ladder-popover {
  max-height: 360px;
  overflow-y: auto;
  padding: var(--spacing-3);
}

.ladder-header {
  font-weight: 600;
  font-size: 0.875rem;
  margin-bottom: var(--spacing-3);
  color: var(--claw-text-primary);
  padding-bottom: var(--spacing-2);
  border-bottom: 1px solid var(--claw-border-light);
}

.ladder-row {
  display: flex;
  align-items: center;
  margin-bottom: var(--spacing-2);
  gap: var(--spacing-3);
}

.ladder-height {
  min-width: 48px;
  font-weight: 600;
  text-align: right;
  font-size: 0.875rem;
}

.ladder-stocks {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-1);
}

.ladder-note {
  font-size: 0.75rem;
  color: var(--claw-text-muted);
  margin-bottom: var(--spacing-2);
}

.pagination-wrap {
  display: flex;
  justify-content: center;
  margin-top: var(--spacing-4);
}

@media (max-width: 767px) {
  .state-filters {
    :deep(.el-radio-button__inner) {
      height: 26px;
      line-height: 24px;
      padding: 0 8px;
      font-size: 0.75rem;
    }
  }
}
</style>
