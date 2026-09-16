<template>
  <div class="page-container">
    <div class="page-shell commodity-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title"><el-icon class="title-icon"><Connection /></el-icon>商品联动</h2>
          <div class="page-subtitle">按短线价格信号拆分上游受益、下游受益与承压股票</div>
        </div>
        <div class="hero-actions">
          <div class="hero-chip">
            <el-icon><DataLine /></el-icon>
            <span>{{ updatedText }}</span>
          </div>
          <el-button :loading="loading" @click="loadData(true)">
            <el-icon><Refresh /></el-icon>
            刷新
          </el-button>
        </div>
      </div>

      <div v-if="sourceWarnings.length" class="source-warnings">
        <el-icon><Warning /></el-icon>
        <span v-for="warning in sourceWarnings" :key="warning.source + warning.message">
          {{ warning.source }}：{{ warning.message }}
        </span>
      </div>

      <section class="summary-grid">
        <div class="stat-card">
          <div class="metric-head"><el-icon><Connection /></el-icon><span>期股信号</span></div>
          <div class="stat-value">{{ futuresRows.length }}</div>
        </div>
        <div class="stat-card">
          <div class="metric-head"><el-icon><Goods /></el-icon><span>现货信号</span></div>
          <div class="stat-value">{{ spotRows.length }}</div>
        </div>
        <div class="stat-card">
          <div class="metric-head"><el-icon><Share /></el-icon><span>产业链</span></div>
          <div class="stat-value">{{ industryRows.length }}</div>
        </div>
        <div class="stat-card highlight-card">
          <div class="metric-head"><el-icon><TopRight /></el-icon><span>最强商品</span></div>
          <div class="stat-value small">{{ topSignal?.indicator_name || '--' }}</div>
          <div class="stat-note" :class="changeClass(topSignal?.effective_change_pct)">
            {{ formatPct(topSignal?.effective_change_pct) }} · {{ topSignal?.signal_basis || '--' }}
          </div>
        </div>
      </section>

      <section class="section-block">
        <el-tabs v-model="activeTab" class="linkage-tabs">
          <el-tab-pane label="期股联动" name="futures" />
          <el-tab-pane label="现货联动" name="spot" />
          <el-tab-pane label="产业链传导" name="industry" />
        </el-tabs>

        <div v-if="activeRows.length" class="signal-grid">
          <article v-for="row in activeRows" :key="row.category + '-' + row.indicator_name + '-' + row.symbol" class="signal-card">
            <div class="signal-main">
              <div class="signal-title-row">
                <div>
                  <div class="signal-title">{{ row.indicator_name }}</div>
                  <div class="signal-subtitle">{{ row.indicator_type || row.symbol || '--' }} · {{ row.signal_basis || row.period }}</div>
                </div>
                <el-tag :type="row.linkage_strength >= 70 ? 'danger' : row.linkage_strength >= 40 ? 'warning' : 'info'" size="small">
                  强度 {{ row.linkage_strength ?? '--' }}
                </el-tag>
              </div>

              <div class="price-strip" :class="trendClass(row)">
                <div>
                  <span class="price-label">有效涨跌</span>
                  <strong :class="changeClass(row.effective_change_pct)">{{ formatPct(row.effective_change_pct) }}</strong>
                </div>
                <div>
                  <span class="price-label">最新值</span>
                  <strong>{{ formatValue(row.latest_value) }}</strong>
                </div>
                <div>
                  <span class="price-label">方向</span>
                  <strong>{{ row.impact_direction }}</strong>
                </div>
              </div>

              <div class="impact-pair">
                <div class="impact-box benefit">
                  <span>上游</span>
                  <strong>{{ row.upstream_impact }}</strong>
                </div>
                <div class="impact-box pressure">
                  <span>下游</span>
                  <strong>{{ row.downstream_impact }}</strong>
                </div>
              </div>

              <div class="meta-line">
                <span>{{ row.target_sector }}</span>
                <span>{{ row.target_etf }}</span>
                <span>{{ formatMiniHistory(row.history) }}</span>
              </div>
              <p class="reason">{{ row.reason }}</p>
            </div>

            <div class="stock-groups">
              <div class="stock-group benefit">
                <div class="group-title">受益股票</div>
                <div v-if="row.benefit_stocks?.length" class="stock-tags">
                  <el-tag
                    v-for="stock in row.benefit_stocks"
                    :key="'b-' + row.indicator_name + '-' + stock.code"
                    size="small"
                    effect="plain"
                    type="danger"
                  >
                    {{ stock.role_label }} {{ stock.name }} {{ stock.code }} {{ formatPct(stock.change_pct) }}{{ stockRiskText(stock) }}
                  </el-tag>
                </div>
                <span v-else class="muted">暂无受益映射</span>
              </div>

              <div class="stock-group pressure">
                <div class="group-title">承压股票</div>
                <div v-if="row.pressure_stocks?.length" class="stock-tags">
                  <el-tag
                    v-for="stock in row.pressure_stocks"
                    :key="'p-' + row.indicator_name + '-' + stock.code"
                    size="small"
                    effect="plain"
                    type="success"
                  >
                    {{ stock.role_label }} {{ stock.name }} {{ stock.code }} {{ formatPct(stock.change_pct) }}{{ stockRiskText(stock) }}
                  </el-tag>
                </div>
                <span v-else class="muted">暂无承压映射</span>
              </div>
            </div>
          </article>
        </div>
        <el-empty v-else :description="emptyText" :image-size="72" />
      </section>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { getCommodityLinkageSummary } from '@/api'

const loading = ref(false)
const activeTab = ref('futures')
const summary = ref({ tabs: { futures: [], spot: [], industry: [] } })

const futuresRows = computed(() => summary.value.tabs?.futures || [])
const spotRows = computed(() => summary.value.tabs?.spot || [])
const industryRows = computed(() => summary.value.tabs?.industry || [])
const activeRows = computed(() => summary.value.tabs?.[activeTab.value] || [])
const allRows = computed(() => [...futuresRows.value, ...spotRows.value])
const rankedRows = computed(() => [...allRows.value].sort((a, b) => {
  const strengthGap = (b.linkage_strength || 0) - (a.linkage_strength || 0)
  if (strengthGap !== 0) return strengthGap
  return Math.abs(b.effective_change_pct || 0) - Math.abs(a.effective_change_pct || 0)
}))
const topSignal = computed(() => rankedRows.value[0])
const sourceWarnings = computed(() => summary.value.warnings || [])
const updatedText = computed(() => summary.value.updated_at ? `更新 ${summary.value.updated_at.slice(11, 16)} · ${cacheLabel.value}` : '等待数据')
const cacheLabel = computed(() => summary.value.cache === 'hit' ? '缓存命中' : summary.value.cache === 'refresh' ? '已刷新' : '--')
const emptyText = computed(() => {
  if (activeTab.value === 'futures') return '暂无期货联动信号'
  if (activeTab.value === 'spot') return '暂无现货联动信号'
  return '暂无产业链传导信号'
})

function changeClass(value) {
  if (value > 0) return 'text-red'
  if (value < 0) return 'text-green'
  return 'muted'
}

function trendClass(row) {
  if (row?.trend_side === 'up') return 'trend-up'
  if (row?.trend_side === 'down') return 'trend-down'
  return 'trend-neutral'
}

function formatPct(value) {
  if (value === null || value === undefined) return '--'
  const sign = value > 0 ? '+' : ''
  return `${sign}${Number(value).toFixed(2)}%`
}

function formatValue(value) {
  if (value === null || value === undefined) return '--'
  return Number(value).toLocaleString('zh-CN', { maximumFractionDigits: 2 })
}

function formatMiniHistory(history = []) {
  const items = history.filter(item => item.value !== null && item.value !== undefined).slice(-5)
  if (!items.length) return '暂无日频'
  return items.map(item => `${String(item.date).slice(4)} ${formatPct(item.change_pct)}`).join(' / ')
}

function stockRiskText(stock) {
  const labels = stock?.risk_labels || []
  if (stock?.is_tradeable === false && !labels.length) return ' [观察/屏蔽]'
  if (!labels.length) return ''
  return ` [${labels.join('/')}]`
}

async function loadData(refresh = false) {
  loading.value = true
  try {
    summary.value = await getCommodityLinkageSummary({ refresh })
  } finally {
    loading.value = false
  }
}

onMounted(() => loadData(false))
</script>

<style scoped lang="scss">
.commodity-page { display: flex; flex-direction: column; gap: 18px; }
.hero-actions { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.source-warnings {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 12px;
  color: #d97706;
  background: rgba(217, 119, 6, 0.08);
  border: 1px solid rgba(217, 119, 6, 0.2);
  border-radius: 8px;
  font-size: 12px;
}
.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}
.stat-card {
  padding: 16px;
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: 8px;
  box-shadow: var(--claw-shadow-sm);
}
.metric-head {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--claw-text-muted);
  font-size: 13px;
  margin-bottom: 10px;
}
.stat-value { font-size: 24px; font-weight: 700; color: var(--claw-text-primary); }
.stat-value.small { font-size: 18px; line-height: 1.25; }
.stat-note { margin-top: 6px; font-size: 13px; }
.signal-grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
.signal-card {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(320px, 0.9fr);
  gap: 16px;
  padding: 16px;
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: 8px;
  box-shadow: var(--claw-shadow-sm);
}
.signal-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.signal-title { font-size: 18px; font-weight: 800; color: var(--claw-text-primary); }
.signal-subtitle { margin-top: 4px; color: var(--claw-text-muted); font-size: 12px; }
.price-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 10px;
  margin-top: 14px;
  padding: 12px;
  border-radius: 8px;
  background: var(--claw-bg-elevated);
  border: 1px solid var(--claw-border-light);
}
.price-strip.trend-up { background: rgba(229, 72, 77, 0.08); }
.price-strip.trend-down { background: rgba(22, 163, 74, 0.08); }
.price-label { display: block; margin-bottom: 4px; color: var(--claw-text-muted); font-size: 12px; }
.impact-pair { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
.impact-box {
  padding: 10px 12px;
  border: 1px solid var(--claw-border);
  border-radius: 8px;
  span { display: block; color: var(--claw-text-muted); font-size: 12px; margin-bottom: 4px; }
  strong { font-size: 15px; }
}
.meta-line { display: flex; flex-wrap: wrap; gap: 8px 14px; margin-top: 12px; color: var(--claw-text-muted); font-size: 12px; }
.reason { margin: 10px 0 0; color: var(--claw-text-secondary); font-size: 13px; line-height: 1.6; }
.stock-groups { display: grid; grid-template-columns: 1fr; gap: 12px; }
.stock-group {
  padding: 12px;
  border: 1px solid var(--claw-border);
  border-radius: 8px;
  background: var(--claw-bg-elevated);
}
.group-title { margin-bottom: 8px; font-weight: 700; color: var(--claw-text-primary); }
.stock-tags { display: flex; gap: 6px; flex-wrap: wrap; }
.text-red { color: #e5484d; font-weight: 700; }
.text-green { color: #16a34a; font-weight: 700; }
.muted { color: var(--claw-text-muted); }
:deep(.el-tabs__header) { margin-bottom: 12px; }
@media (max-width: 1100px) {
  .summary-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .signal-card { grid-template-columns: 1fr; }
}
@media (max-width: 620px) {
  .summary-grid,
  .price-strip,
  .impact-pair { grid-template-columns: 1fr; }
}
</style>
