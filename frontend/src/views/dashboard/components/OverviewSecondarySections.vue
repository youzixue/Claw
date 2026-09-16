<template>
  <div class="secondary-sections">
    <section v-if="conclusions.length" class="section-block">
      <div class="section-title section-title-inline">
        <span>关键判断</span>
        <span class="auto-refresh-tip">每 45 秒自动刷新</span>
      </div>
      <div class="conclusion-grid">
        <div v-for="item in conclusions" :key="item.key" class="conclusion-card" :class="`tone-${item.tone || 'neutral'}`">
          <div class="conclusion-label">{{ item.label }}</div>
          <div class="conclusion-value">{{ item.value }}</div>
          <div class="conclusion-note">{{ item.note || '--' }}</div>
        </div>
      </div>
    </section>

    <section v-if="factorRows.length" class="section-block">
      <div class="section-title">外部联动因子</div>
      <div class="factor-list-group">
        <div class="factor-list-head">
          <span>盘口联动</span>
          <el-tag size="small" effect="dark">{{ factorRows.length }} 项</el-tag>
        </div>
        <div class="factor-list-table">
          <div class="factor-list-row factor-list-row-head">
            <span>分组</span>
            <span>因子</span>
            <span>市场</span>
            <span>指数</span>
            <span>涨跌</span>
            <span>时间</span>
          </div>
          <div v-for="item in factorRows" :key="item.key" class="factor-list-row">
            <span class="factor-group">{{ item.group_label }}</span>
            <span class="factor-name">{{ item.label }}</span>
            <span class="factor-market">{{ item.market || '--' }}</span>
            <span class="factor-price" :class="changeColorClass(item.change_pct)">
              {{ formatFactorValue(item) }}
            </span>
            <span class="factor-change" :class="changeColorClass(item.change_pct)">
              {{ formatChange(item.change_pct) }}
            </span>
            <span class="factor-time">{{ formatFactorTime(item) }}</span>
          </div>
        </div>
      </div>
    </section>

    <section v-if="mappingInsights.length" class="section-block">
      <div class="section-title">外盘主题 → A股映射</div>
      <div class="mapping-grid">
        <div v-for="item in mappingInsights" :key="item.source_key" class="mapping-card" :class="`mapping-${item.status || 'pending'}`">
          <div class="mapping-header">
            <div>
              <div class="mapping-title">{{ item.source_label }}</div>
              <div class="mapping-themes">
                <el-tag v-for="theme in item.a_share_themes" :key="theme" size="small" effect="light" class="theme-tag">
                  {{ theme }}
                </el-tag>
              </div>
            </div>
            <el-tag :type="mappingStatusType(item.status)" effect="light" round>
              {{ mappingStatusLabel(item.status) }}
            </el-tag>
          </div>
          <div class="mapping-note">{{ item.note || '--' }}</div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
defineProps({
  conclusions: { type: Array, default: () => [] },
  factorRows: { type: Array, default: () => [] },
  mappingInsights: { type: Array, default: () => [] },
  mappingStatusLabel: { type: Function, required: true },
  mappingStatusType: { type: Function, required: true },
  formatFactorValue: { type: Function, required: true },
  formatFactorTime: { type: Function, required: true },
  formatChange: { type: Function, required: true },
  changeColorClass: { type: Function, required: true },
})
</script>

<style scoped lang="scss">
.secondary-sections {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.section-block {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.section-title {
  position: relative;
  padding-left: 10px;
  font-size: 15px;
  font-weight: 700;
  color: var(--claw-text-primary);
}

.section-title::before {
  content: '';
  position: absolute;
  left: 0;
  top: 50%;
  width: 3px;
  height: 14px;
  transform: translateY(-50%);
  border-radius: 2px;
  background: linear-gradient(180deg, #38bdf8, #2563eb);
}

.section-title-inline {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.conclusion-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 8px;
}

.conclusion-card {
  border-radius: 10px;
  padding: 10px 12px;
  border: 1px solid var(--claw-border);
  background: var(--claw-bg-card);
}

.conclusion-label {
  color: var(--claw-text-muted);
  font-size: 12px;
}

.conclusion-value {
  font-size: 24px;
  font-weight: 700;
  margin: 4px 0;
  line-height: 1.15;
  color: var(--claw-text-primary);
}

.conclusion-note {
  font-size: 12px;
  color: var(--claw-text-secondary);
}

.tone-positive {
  border-color: rgba(225, 82, 72, 0.35);
  background: rgba(225, 82, 72, 0.08);
}

.tone-negative {
  border-color: rgba(34, 197, 94, 0.35);
  background: rgba(34, 197, 94, 0.08);
}

.tone-warning {
  border-color: rgba(230, 162, 60, 0.35);
  background: rgba(230, 162, 60, 0.08);
}

.factor-list-group {
  overflow: hidden;
  padding: 0;
}

.factor-list-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 6px 10px;
  border-bottom: 1px solid var(--claw-border-light);
  background: var(--neutral-50);
  color: var(--claw-text-muted);
  font-size: 12px;
}

.factor-list-row {
  display: grid;
  grid-template-columns: 0.9fr 1.4fr 0.7fr 0.8fr 0.8fr 1.2fr;
  gap: 8px;
  align-items: center;
  padding: 8px 12px;
  border-bottom: 1px solid var(--claw-border-light);
  font-size: 13px;
}

.factor-list-row-head {
  background: var(--neutral-50);
  color: var(--claw-text-muted);
  font-size: 12px;
  font-weight: 600;
}

.factor-list-row:last-child {
  border-bottom: 0;
}

.factor-list-row span {
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.factor-list-row:nth-child(odd):not(.factor-list-row-head) {
  background: rgba(245, 247, 250, 0.62);
}

.factor-list-row:hover {
  background: rgba(236, 245, 255, 0.72);
}

.factor-name {
  font-weight: 600;
  color: var(--claw-text-primary);
}

.factor-group,
.factor-market,
.factor-time {
  color: var(--claw-text-secondary);
}

.mapping-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}

.mapping-card {
  padding: 12px;
  border-left: 3px solid rgba(148, 163, 184, 0.28);
}

.mapping-synced {
  border-left-color: var(--claw-up);
}

.mapping-diverging {
  border-left-color: var(--claw-down);
}

.mapping-lagging {
  border-left-color: var(--claw-warning);
}

.mapping-header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 10px;
  margin-bottom: 8px;
}

.mapping-title {
  font-size: 15px;
  font-weight: 700;
  margin-bottom: 6px;
  color: var(--claw-text-primary);
}

.mapping-themes {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.mapping-note,
.auto-refresh-tip {
  color: var(--claw-text-muted, #909399);
  font-size: 12px;
}

.mapping-note {
  line-height: 1.55;
  color: var(--claw-text-secondary);
}

@media (max-width: 1365px) {
  .conclusion-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .mapping-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 768px) {
  .section-title-inline {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }

  .conclusion-grid {
    grid-template-columns: 1fr;
  }

  .factor-list-row {
    grid-template-columns: 1fr 1fr;
  }

  .factor-list-row > span:nth-child(n + 3) {
    display: none;
  }
}
</style>
