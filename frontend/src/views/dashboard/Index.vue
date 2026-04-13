<template>
  <div class="page-container overview-v2-page">
    <div class="page-head">
      <div>
        <h2 class="page-title">🦅 Dashboard 2.0</h2>
        <div class="page-subtitle">跨市场联动总览，自动读取后台快照</div>
      </div>
      <div class="snapshot-time">
        <span class="time-label">快照时间</span>
        <span class="time-value">{{ formatDate(snapshotTime, 'MM-DD HH:mm:ss') }}</span>
      </div>
    </div>

    <div v-if="loading" class="loading-wrapper">
      <el-skeleton :rows="10" animated />
    </div>

    <template v-else>
      <div class="section-title section-title-inline">
        <span>总评摘要</span>
        <span class="refresh-status" :class="refreshStatusClass">{{ refreshStatusText }}</span>
      </div>
      <div class="summary-card">
        {{ summaryText || '暂无总评' }}
      </div>

      <div class="section-title">风险 / 机会聚焦</div>
      <div class="focus-strip-grid">
        <div v-for="item in focusStrips" :key="item.kind" class="focus-strip" :class="`focus-${item.kind} focus-tone-${item.tone || 'neutral'}`">
          <div class="focus-kicker">{{ item.kind === 'opportunity' ? '机会方向' : '风险方向' }}</div>
          <div class="focus-label">{{ item.label }}</div>
          <div class="focus-detail">{{ item.detail || '--' }}</div>
        </div>
      </div>

      <div class="section-title">A股核心状态</div>
      <div class="a-share-core-card">
        <div class="core-index-grid">
          <div v-for="item in aShareCore.indices || []" :key="item.code" class="stat-card factor-card">
            <div class="stat-label">{{ item.label }}</div>
            <div class="stat-value big" :class="changeColorClass(item.change_pct)">
              {{ item.price != null ? Number(item.price).toFixed(2) : '--' }}
            </div>
            <div class="stat-sub" :class="changeColorClass(item.change_pct)">
              {{ formatChange(item.change_pct) }}
            </div>
          </div>
        </div>
        <div class="core-metrics-grid">
          <div class="metric-item">
            <span class="metric-label">情绪周期</span>
            <span class="metric-value">{{ sentimentCycleLabel(aShareCore.sentiment_cycle) }}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">涨停 / 跌停</span>
            <span class="metric-value"><span class="text-red">{{ aShareCore.limit_up_count ?? '--' }}</span> / <span class="text-green">{{ aShareCore.limit_down_count ?? '--' }}</span></span>
          </div>
          <div class="metric-item">
            <span class="metric-label">封板率</span>
            <span class="metric-value">{{ aShareCore.seal_rate != null ? Number(aShareCore.seal_rate).toFixed(1) + '%' : '--' }}</span>
          </div>
          <div class="metric-item">
            <span class="metric-label">最高连板</span>
            <span class="metric-value">{{ aShareCore.board_height ?? '--' }}</span>
          </div>
          <div class="metric-item metric-item-wide">
            <span class="metric-label">主力净流入</span>
            <span class="metric-value" :class="changeColorClass(aShareCore.main_net_inflow)">{{ formatAmountYi(aShareCore.main_net_inflow) }}</span>
          </div>
        </div>
      </div>

      <div class="section-title section-title-inline">
        <span>结论卡片</span>
        <span class="auto-refresh-tip">每 45 秒自动刷新</span>
      </div>
      <div class="conclusion-grid">
        <div v-for="item in conclusions" :key="item.key" class="conclusion-card" :class="`tone-${item.tone || 'neutral'}`">
          <div class="stat-label">{{ item.label }}</div>
          <div class="conclusion-value">{{ item.value }}</div>
          <div class="mapping-note">{{ item.note || '--' }}</div>
        </div>
      </div>

      <div class="section-title">外部联动因子</div>
      <div class="factor-groups">
        <div v-for="group in groupedFactors" :key="group.key" class="factor-group-card">
          <div class="group-title">{{ group.label }}</div>
          <div class="factor-grid">
            <div v-for="item in group.items" :key="item.key" class="stat-card factor-card">
              <div class="stat-label">{{ item.label }}</div>
              <div class="stat-value big" :class="changeColorClass(item.change_pct)">
                {{ item.price != null ? Number(item.price).toFixed(2) : '--' }}
              </div>
              <div class="stat-sub" :class="changeColorClass(item.change_pct)">
                {{ formatChange(item.change_pct) }}
              </div>
              <div class="factor-meta">
                <span class="market-tag">{{ item.market || '--' }}</span>
                <span class="stat-date">{{ item.trade_time || '--' }}</span>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-if="externalFactors.length === 0" description="暂无外部联动数据" :image-size="40" />
      </div>

      <div class="section-title">外盘主题 → A股映射</div>
      <div class="mapping-list">
        <div v-for="item in mappingInsights" :key="item.source_key" class="mapping-card">
          <div class="mapping-head">
            <div>
              <div class="mapping-title">{{ item.source_label }}</div>
              <div class="mapping-themes">
                <el-tag v-for="theme in item.a_share_themes" :key="theme" size="small" effect="plain" class="theme-tag">
                  {{ theme }}
                </el-tag>
              </div>
            </div>
            <el-tag :type="mappingStatusType(item.status)" round>
              {{ mappingStatusLabel(item.status) }}
            </el-tag>
          </div>
          <div class="mapping-note">{{ item.note || '--' }}</div>
        </div>
        <el-empty v-if="mappingInsights.length === 0" description="暂无映射判断" :image-size="40" />
      </div>
    </template>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-top: 16px" />
  </div>
</template>

<script setup>
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import dayjs from 'dayjs'
import { getDashboardOverviewV2 } from '@/api'
import { changeColorClass, formatChange, formatDate, sentimentCycleLabel } from '@/composables/useUtils'

const REFRESH_MS = 45 * 1000

const data = ref({
  snapshot_time: null,
  summary_text: '',
  conclusions: [],
  focus_strips: [],
  a_share_core: { indices: [] },
  external_factors: [],
  mapping_insights: [],
})
const loading = ref(true)
const error = ref('')
const refreshing = ref(false)
const lastRefreshAt = ref(null)
const lastRefreshFailedAt = ref(null)
let refreshTimer = null

const snapshotTime = computed(() => data.value.snapshot_time)
const summaryText = computed(() => data.value.summary_text || '')
const conclusions = computed(() => data.value.conclusions || [])
const focusStrips = computed(() => data.value.focus_strips || [])
const aShareCore = computed(() => data.value.a_share_core || { indices: [] })
const externalFactors = computed(() => data.value.external_factors || [])
const mappingInsights = computed(() => data.value.mapping_insights || [])

const refreshStatusText = computed(() => {
  if (refreshing.value) return '刷新中...'
  if (lastRefreshFailedAt.value) return `刷新失败，${dayjs(lastRefreshFailedAt.value).format('HH:mm:ss')} 后将自动重试`
  if (!lastRefreshAt.value) return `每 ${REFRESH_MS / 1000} 秒自动刷新`
  const diffSec = dayjs().diff(lastRefreshAt.value, 'second')
  if (diffSec < 5) return '刚刚更新'
  return `${diffSec} 秒前更新`
})

const refreshStatusClass = computed(() => {
  if (refreshing.value) return 'status-refreshing'
  if (lastRefreshFailedAt.value) return 'status-failed'
  return 'status-ok'
})

const factorGroupMeta = {
  HK: { key: 'hk', label: '港股' },
  US: { key: 'us', label: '美股' },
  US_CN: { key: 'us_cn', label: '中概' },
  A50: { key: 'a50', label: 'A50 / 权重' },
  US_RATE: { key: 'macro', label: '宏观' },
  CMDTY: { key: 'commodity', label: '商品' },
  FX: { key: 'fx', label: '汇率' },
}

const groupedFactors = computed(() => {
  const groups = new Map()
  externalFactors.value.forEach((item) => {
    const meta = factorGroupMeta[item.market] || { key: 'other', label: '其他' }
    if (!groups.has(meta.key)) groups.set(meta.key, { ...meta, items: [] })
    groups.get(meta.key).items.push(item)
  })
  return Array.from(groups.values())
})

const mappingStatusLabel = (status) => {
  const map = { synced: '同步', diverging: '背离', lagging: '滞后', missing: '缺失', pending: '待定' }
  return map[status] || status || '--'
}

const mappingStatusType = (status) => {
  const map = { synced: 'success', diverging: 'danger', lagging: 'warning', missing: 'info', pending: 'info' }
  return map[status] || 'info'
}

const formatAmountYi = (val) => {
  if (val == null || Number.isNaN(Number(val))) return '--'
  return `${Number(val).toFixed(2)} 亿`
}

const loadData = async ({ silent = false } = {}) => {
  if (refreshing.value) return
  try {
    refreshing.value = true
    if (!silent) loading.value = true
    const res = await getDashboardOverviewV2()
    data.value = res
    error.value = ''
    lastRefreshAt.value = dayjs()
    lastRefreshFailedAt.value = null
  } catch (e) {
    if (!silent) error.value = 'Dashboard 2.0 加载失败'
    lastRefreshFailedAt.value = dayjs()
    console.error('overview-v2 数据加载失败:', e)
  } finally {
    if (!silent) loading.value = false
    refreshing.value = false
  }
}

onMounted(async () => {
  await loadData()
  refreshTimer = window.setInterval(() => {
    loadData({ silent: true })
  }, REFRESH_MS)
})

onBeforeUnmount(() => {
  if (refreshTimer) {
    window.clearInterval(refreshTimer)
    refreshTimer = null
  }
})
</script>

<style scoped lang="scss">
.overview-v2-page { display: flex; flex-direction: column; gap: 16px; }
.page-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; }
.page-subtitle { color: var(--claw-text-muted, #909399); font-size: 13px; margin-top: 4px; }
.snapshot-time { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; }
.time-label { color: var(--claw-text-muted, #909399); font-size: 12px; }
.time-value { font-size: 13px; color: var(--el-text-color-primary); }
.summary-card { background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 10px; padding: 16px; line-height: 1.8; font-size: 15px; }
.focus-strip-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.focus-strip { border-radius: 10px; padding: 16px; border: 1px solid var(--claw-border); }
.focus-kicker { font-size: 12px; color: var(--claw-text-muted, #909399); margin-bottom: 8px; }
.focus-label { font-size: 18px; font-weight: 700; margin-bottom: 8px; }
.focus-detail { color: var(--claw-text-muted, #909399); line-height: 1.6; }
.focus-opportunity { background: rgba(103, 194, 58, 0.08); border-color: rgba(103, 194, 58, 0.35); }
.focus-risk { background: rgba(245, 108, 108, 0.08); border-color: rgba(245, 108, 108, 0.35); }
.a-share-core-card, .factor-group-card, .mapping-card { background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 10px; padding: 16px; }
.core-index-grid, .factor-grid, .conclusion-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.core-metrics-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 12px; margin-top: 16px; }
.metric-item { display: flex; flex-direction: column; gap: 6px; padding: 12px; border-radius: 8px; background: rgba(127,127,127,.08); }
.metric-item-wide { grid-column: span 1; }
.metric-label, .stat-date, .mapping-note, .auto-refresh-tip { color: var(--claw-text-muted, #909399); }
.metric-value { font-size: 16px; font-weight: 700; }
.refresh-status { font-size: 12px; }
.status-ok { color: #67c23a; }
.status-refreshing { color: #409eff; }
.status-failed { color: #e6a23c; }
.conclusion-card { border-radius: 10px; padding: 16px; border: 1px solid var(--claw-border); background: var(--claw-bg-card); }
.conclusion-value { font-size: 20px; font-weight: 700; margin: 8px 0 10px; }
.tone-positive { border-color: rgba(103, 194, 58, 0.35); background: rgba(103, 194, 58, 0.08); }
.tone-negative { border-color: rgba(245, 108, 108, 0.35); background: rgba(245, 108, 108, 0.08); }
.tone-warning { border-color: rgba(230, 162, 60, 0.35); background: rgba(230, 162, 60, 0.08); }
.factor-groups { display: flex; flex-direction: column; gap: 16px; }
.group-title, .mapping-title { font-size: 16px; font-weight: 700; margin-bottom: 12px; color: var(--el-text-color-primary); }
.section-title-inline { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
.factor-card { min-height: 132px; }
.factor-meta, .mapping-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.market-tag { display: inline-flex; align-items: center; padding: 2px 8px; border-radius: 999px; background: rgba(64, 158, 255, 0.12); color: #409eff; font-size: 12px; }
.mapping-list { display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px; }
.mapping-themes { display: flex; flex-wrap: wrap; gap: 8px; }
.loading-wrapper { padding: 40px 0; }
@media (max-width: 1200px) { .core-index-grid, .factor-grid, .conclusion-grid { grid-template-columns: repeat(2, 1fr); } .core-metrics-grid, .focus-strip-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 768px) { .page-head, .section-title-inline { flex-direction: column; align-items: flex-start; } .snapshot-time { align-items: flex-start; } .core-index-grid, .factor-grid, .conclusion-grid, .mapping-list, .core-metrics-grid, .focus-strip-grid { grid-template-columns: 1fr; } }
</style>
