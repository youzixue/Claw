<template>
  <div class="page-container">
    <div class="page-shell overview-v2-page">
      <div class="page-hero page-head">
        <div>
          <h2 class="page-title">行情总览</h2>
          <div class="page-subtitle">跨市场联动总览，实时把握A股脉搏</div>
        </div>
        <div class="snapshot-time">
          <span class="time-label">数据更新</span>
          <span class="time-value">{{ formatDate(snapshotTime, 'MM-DD HH:mm:ss') }}</span>
        </div>
      </div>

      <div v-if="loading" class="loading-wrapper panel-card">
        <el-skeleton :rows="10" animated />
      </div>

      <template v-else>
        <OverviewPrimarySections
          :summary-text="summaryText"
          :refresh-status-text="refreshStatusText"
          :refresh-status-class="refreshStatusClass"
          :focus-strips="focusStrips"
          :a-share-core="aShareCore"
          :change-color-class="changeColorClass"
          :format-change="formatChange"
          :format-amount-yi="formatAmountYi"
          :sentiment-cycle-label="sentimentCycleLabel"
          :sentiment-cycle-class="sentimentCycleClass"
        />

        <OverviewSecondarySections
          v-if="showSecondarySections"
          :conclusions="conclusions"
          :factor-rows="factorRows"
          :mapping-insights="mappingInsights"
          :mapping-status-label="mappingStatusLabel"
          :mapping-status-type="mappingStatusType"
          :format-factor-value="formatFactorValue"
          :format-factor-time="formatFactorTime"
          :format-change="formatChange"
          :change-color-class="changeColorClass"
        />
      </template>

      <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" style="margin-top: 16px" />
    </div>
  </div>
</template>

<script setup>
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref } from 'vue'
import dayjs from 'dayjs'
import { getDashboardOverviewV2 } from '@/api'
import { changeColorClass, formatChange, formatDate, sentimentCycleLabel } from '@/composables/useUtils'

const OverviewPrimarySections = defineAsyncComponent(() => import('./components/OverviewPrimarySections.vue'))
const OverviewSecondarySections = defineAsyncComponent(() => import('./components/OverviewSecondarySections.vue'))

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
const showSecondarySections = ref(false)
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

const factorRows = computed(() => {
  const order = Object.values(factorGroupMeta).reduce((acc, cur, idx) => {
    acc[cur.key] = idx
    return acc
  }, {})
  return (externalFactors.value || [])
    .map((item) => {
      const meta = factorGroupMeta[item.market] || { key: 'other', label: '其他' }
      return { ...item, group_key: meta.key, group_label: meta.label, group_order: order[meta.key] ?? 999 }
    })
    .sort((a, b) => a.group_order - b.group_order)
})

const mappingStatusLabel = (status) => {
  const map = { synced: '同步', diverging: '背离', lagging: '滞后', missing: '缺失', pending: '待定' }
  return map[status] || status || '--'
}

const mappingStatusType = (status) => {
  const map = { synced: 'success', diverging: 'danger', lagging: 'warning', missing: 'info', pending: 'info' }
  return map[status] || 'info'
}

const sentimentCycleClass = (cycle) => {
  const map = {
    freezing: 'text-down',
    divergence: 'text-warning',
    recovery: 'text-up',
    climax: 'text-warning',
    pending: 'text-secondary',
    冰点: 'text-down',
    低迷: 'text-secondary',
    复苏: 'text-up',
    活跃: 'text-up',
    狂热: 'text-warning',
  }
  return map[cycle] || ''
}

const formatAmountYi = (val) => {
  if (val == null || Number.isNaN(Number(val))) return '--'
  const num = Number(val)
  const abs = Math.abs(num)
  const sign = num >= 0 ? '+' : '-'
  if (abs >= 100) return `${sign}${abs.toFixed(1)}亿`
  if (abs >= 1) return `${sign}${abs.toFixed(2)}亿`
  if (abs >= 0.01) return `${sign}${(abs * 10000).toFixed(0)}万`
  return `${sign}${abs.toFixed(4)}亿`
}

const formatFactorValue = (item) => {
  const val = item?.price
  if (val == null || Number.isNaN(Number(val))) return '--'
  const num = Number(val)
  if (item?.market === 'FX') return num.toFixed(4)
  if (item?.market === 'US_RATE') return num.toFixed(3)
  return num.toFixed(2)
}

const formatFactorTime = (item) => {
  const raw = item?.trade_time || snapshotTime.value
  if (!raw) return '--'
  const t = dayjs(String(raw).replace('T', ' '))
  return t.isValid() ? t.format('MM-DD HH:mm') : String(raw)
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
    if (!silent) error.value = '行情总览加载失败'
    lastRefreshFailedAt.value = dayjs()
    console.error('overview-v2 数据加载失败:', e)
  } finally {
    if (!silent) loading.value = false
    refreshing.value = false
  }
}

onMounted(async () => {
  await loadData()
  const scheduleSecondary = () => {
    showSecondarySections.value = true
  }
  if (typeof window !== 'undefined' && 'requestIdleCallback' in window) {
    window.requestIdleCallback(scheduleSecondary, { timeout: 400 })
  } else {
    window.setTimeout(scheduleSecondary, 180)
  }
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
.overview-v2-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
}

.page-title {
  margin: 0;
  font-size: 28px;
  line-height: 1.08;
  font-weight: 800;
  letter-spacing: 0;
  color: var(--claw-text-primary);
}

.page-subtitle {
  margin-top: 6px;
  font-size: 13px;
  color: var(--claw-text-secondary);
}

.snapshot-time {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  padding: 8px 12px;
  border-radius: 10px;
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  box-shadow: var(--claw-shadow-sm);
}

.time-label {
  font-size: 12px;
  color: var(--claw-text-muted, #909399);
}

.time-value {
  font-size: 13px;
  color: var(--claw-text-primary);
}

.separator {
  color: #94a3b8;
  margin: 0 4px;
}

.auto-refresh-tip,
.refresh-status {
  color: var(--claw-text-muted, #909399);
  font-size: 12px;
}

.loading-wrapper {
  padding: 40px 0;
}

.focus-strip,
.conclusion-card,
.mapping-card,
.index-card,
.signal-card,
.metric-item,
.factor-list-row {
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
}

.focus-strip:hover,
.conclusion-card:hover,
.mapping-card:hover,
.index-card:hover,
.signal-card:hover {
  box-shadow: var(--shadow-md);
  transform: translateY(-2px);
}

@media (max-width: 1365px) {
}

@media (max-width: 768px) {
  .page-head,
  .section-title-inline {
    flex-direction: column;
    align-items: flex-start;
    gap: 8px;
  }
  .snapshot-time {
    align-items: flex-start;
  }
  .page-title {
    font-size: 22px;
  }
}
</style>
