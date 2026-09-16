<template>
  <div class="primary-sections">
    <section class="section-block summary-block">
      <div class="section-title section-title-inline">
        <span>盘面参考</span>
        <span class="refresh-status" :class="refreshStatusClass">
          <el-icon><RefreshRight /></el-icon>
          {{ refreshStatusText }}
        </span>
      </div>
      <div class="decision-board">
        <div class="summary-card summary-card-hero">
          <div class="summary-icon-wrap">
            <el-icon><TrendCharts /></el-icon>
          </div>
          <div class="summary-content">
            <span class="summary-kicker">今日总评</span>
            <strong>{{ summaryText || '暂无总评数据，系统正在分析市场状态...' }}</strong>
          </div>
        </div>

        <div class="signal-grid">
          <div
            v-for="item in focusStrips"
            :key="item.kind"
            class="signal-card"
            :class="`signal-${item.kind} signal-tone-${item.tone || 'neutral'}`"
          >
            <div class="signal-head">
              <el-icon v-if="item.kind === 'opportunity'"><Opportunity /></el-icon>
              <el-icon v-else><Warning /></el-icon>
              <span>{{ item.kind === 'opportunity' ? '机会线索' : '风险线索' }}</span>
            </div>
            <div class="signal-value">{{ item.label }}</div>
            <div class="signal-note">{{ item.detail || '--' }}</div>
          </div>

          <div class="signal-card signal-neutral">
            <div class="signal-head">
              <el-icon><TrendCharts /></el-icon>
              <span>执行环境</span>
            </div>
            <div class="signal-value" :class="marketEnvClass(aShareCore?.market_environment)">
              {{ marketEnvLabel(aShareCore?.market_environment) }}
            </div>
            <div class="signal-note">
              买入阈值 {{ aShareCore?.buy_threshold ? Number(aShareCore.buy_threshold).toFixed(1) : '--' }}
            </div>
          </div>
        </div>
      </div>
    </section>

    <section v-if="aShareCore" class="section-block">
      <div class="section-title">A股核心状态</div>
      <div class="a-share-core-card">
        <div class="core-index-grid">
          <div v-for="item in aShareCore.indices || []" :key="item.code" class="index-card">
            <div class="index-header">
              <span class="index-name">{{ item.label }}</span>
              <span class="index-code">{{ item.code }}</span>
            </div>
            <div class="index-price" :class="changeColorClass(item.change_pct)">
              {{ item.price != null ? Number(item.price).toFixed(2) : '--' }}
            </div>
            <div class="index-change" :class="changeColorClass(item.change_pct)">
              <el-icon v-if="item.change_pct > 0"><ArrowUp /></el-icon>
              <el-icon v-else-if="item.change_pct < 0"><ArrowDown /></el-icon>
              {{ formatChange(item.change_pct) }}
            </div>
          </div>
        </div>

        <div class="core-metrics-grid">
          <div class="metric-item metric-emphasis">
            <span class="metric-label"><el-icon><TrendCharts /></el-icon>大盘环境</span>
            <span class="metric-value" :class="marketEnvClass(aShareCore.market_environment)">
              {{ marketEnvLabel(aShareCore.market_environment) }}
            </span>
          </div>
          <div class="metric-item">
            <span class="metric-label"><el-icon><Sunny /></el-icon>情绪周期</span>
            <span class="metric-value" :class="sentimentCycleClass(aShareCore.sentiment_cycle)">
              {{ sentimentCycleLabel(aShareCore.sentiment_cycle) }}
            </span>
          </div>
          <div class="metric-item">
            <span class="metric-label"><el-icon><Histogram /></el-icon>涨停 / 跌停</span>
            <span class="metric-value">
              <span class="text-up">{{ aShareCore.limit_up_count ?? '--' }}</span>
              <span class="separator">/</span>
              <span class="text-down">{{ aShareCore.limit_down_count ?? '--' }}</span>
            </span>
          </div>
          <div class="metric-item">
            <span class="metric-label"><el-icon><Finished /></el-icon>封板率</span>
            <span class="metric-value">
              {{ aShareCore.seal_rate != null ? Number(aShareCore.seal_rate).toFixed(1) + '%' : '--' }}
            </span>
          </div>
          <div class="metric-item">
            <span class="metric-label"><el-icon><Top /></el-icon>最高连板</span>
            <span class="metric-value highlight">{{ aShareCore.board_height ?? '--' }}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label"><el-icon><Coin /></el-icon>东财主力资金</span>
            <span class="metric-value" :class="changeColorClass(aShareCore.main_net_inflow)">
              {{ formatAmountYi(aShareCore.main_net_inflow) }}
            </span>
            <span class="metric-note">主力 = 超大单 + 大单</span>
          </div>
          <div class="metric-item" v-if="aShareCore.buy_threshold">
            <span class="metric-label"><el-icon><Aim /></el-icon>买入阈值</span>
            <span class="metric-value">{{ aShareCore.buy_threshold.toFixed(1) }}</span>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<script setup>
import {
  RefreshRight,
  Opportunity,
  Warning,
  ArrowUp,
  ArrowDown,
  Sunny,
  Histogram,
  Finished,
  Top,
  Coin,
  TrendCharts,
  Aim,
} from '@element-plus/icons-vue'

// V2.2融合: 大盘环境标签
const marketEnvLabel = (env) => ({ strong: '强势', neutral: '震荡', weak: '弱势' }[env] || env || '--')
const marketEnvClass = (env) => ({ strong: 'text-up', neutral: '', weak: 'text-down' }[env] || '')

defineProps({
  summaryText: { type: String, default: '' },
  refreshStatusText: { type: String, default: '' },
  refreshStatusClass: { type: String, default: '' },
  focusStrips: { type: Array, default: () => [] },
  aShareCore: { type: Object, default: () => ({ indices: [] }) },
  changeColorClass: { type: Function, required: true },
  formatChange: { type: Function, required: true },
  formatAmountYi: { type: Function, required: true },
  sentimentCycleLabel: { type: Function, required: true },
  sentimentCycleClass: { type: Function, required: true },
})
</script>

<style scoped lang="scss">
.primary-sections {
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

.decision-board {
  display: grid;
  grid-template-columns: minmax(0, 1.25fr) minmax(360px, 0.75fr);
  gap: 12px;
}

.summary-card-hero {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 16px;
  min-height: 150px;
  background: linear-gradient(135deg, rgba(236, 245, 255, 0.95), var(--claw-bg-card));
}

.summary-icon-wrap {
  width: 38px;
  height: 38px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  background: rgba(64, 158, 255, 0.12);
  color: var(--claw-primary);
  border: 1px solid rgba(64, 158, 255, 0.16);
  flex-shrink: 0;
}

.summary-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 10px;
  line-height: 1.7;
  font-size: 15px;
  color: var(--claw-text-primary);
}

.summary-kicker {
  width: fit-content;
  padding: 3px 8px;
  border-radius: 6px;
  background: rgba(64, 158, 255, 0.1);
  color: var(--claw-primary);
  font-size: 12px;
  font-weight: 600;
}

.signal-grid {
  display: grid;
  grid-template-columns: 1fr;
  gap: 10px;
}

.signal-card {
  min-height: 0;
  padding: 12px;
  border-radius: 10px;
  border: 1px solid var(--claw-border);
  background: var(--claw-bg-card);
}

.signal-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
  color: var(--claw-text-muted);
  font-size: 12px;
  font-weight: 600;
}

.signal-value {
  color: var(--claw-text-primary);
  font-size: 17px;
  font-weight: 800;
  line-height: 1.25;
}

.signal-note {
  margin-top: 6px;
  color: var(--claw-text-secondary);
  font-size: 12px;
  line-height: 1.45;
}

.signal-opportunity {
  border-color: rgba(225, 82, 72, 0.24);
  background: linear-gradient(135deg, rgba(225, 82, 72, 0.06), var(--claw-bg-card));
}

.signal-risk {
  border-color: rgba(34, 197, 94, 0.24);
  background: linear-gradient(135deg, rgba(34, 197, 94, 0.06), var(--claw-bg-card));
}

.focus-strip-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
}

.focus-strip {
  border-radius: 10px;
  padding: 12px;
  border: 1px solid var(--claw-border);
}

.focus-kicker {
  font-size: 12px;
  color: var(--claw-text-muted, #909399);
  margin-bottom: 6px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.focus-label {
  font-size: 17px;
  font-weight: 700;
  margin-bottom: 6px;
  color: var(--claw-text-primary);
}

.focus-detail {
  color: var(--claw-text-secondary);
  font-size: 13px;
}

.focus-opportunity {
  background: rgba(103, 194, 58, 0.08);
  border-color: rgba(103, 194, 58, 0.35);
}

.focus-risk {
  background: rgba(245, 108, 108, 0.08);
  border-color: rgba(245, 108, 108, 0.35);
}

.a-share-core-card {
  display: grid;
  grid-template-columns: minmax(320px, 0.9fr) minmax(0, 1.1fr);
  gap: 12px;
  padding: 14px;
}

.core-index-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
}

.index-card {
  border-radius: 8px;
  border: 1px solid var(--claw-border-light);
  background: linear-gradient(180deg, var(--claw-bg-card), var(--neutral-50));
  padding: 12px;
  text-align: left;
}

.index-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 6px;
}

.index-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--claw-text-secondary);
}

.index-code {
  font-size: 11px;
  color: var(--claw-text-muted);
  border: 1px solid var(--claw-border-light);
  background: var(--neutral-50);
  padding: 2px 6px;
  border-radius: 6px;
}

.index-price {
  font-size: 22px;
  font-weight: 700;
}

.index-change {
  font-size: 13px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.core-metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}

.metric-item {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--claw-border-light);
  background: var(--neutral-50);
}

.metric-emphasis {
  border-color: rgba(225, 82, 72, 0.28);
}

.metric-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--claw-text-muted, #909399);
}

.metric-value {
  font-size: 16px;
  font-weight: 700;
  color: var(--claw-text-primary);
}

.metric-note {
  font-size: 11px;
  line-height: 1.4;
  color: #8a97ad;
}

.separator {
  color: #94a3b8;
  margin: 0 4px;
}

.highlight {
  color: var(--claw-primary);
}

.refresh-status {
  color: var(--claw-text-muted, #909399);
  font-size: 12px;
}

@media (max-width: 1365px) {
  .decision-board {
    grid-template-columns: 1fr;
  }

  .signal-grid {
    grid-template-columns: repeat(3, 1fr);
  }

  .a-share-core-card {
    grid-template-columns: 1fr;
  }

  .core-index-grid {
    grid-template-columns: repeat(2, 1fr);
  }

  .core-metrics-grid {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 768px) {
  .section-title-inline {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }

  .focus-strip-grid,
  .signal-grid,
  .core-index-grid,
  .core-metrics-grid {
    grid-template-columns: 1fr;
  }
}
</style>
