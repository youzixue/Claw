<template>
  <div class="page-container">
    <div class="page-shell tenbagger-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title"><el-icon class="title-icon"><Aim /></el-icon>牛股雷达</h2>
          <div class="page-subtitle">异动快照监控 · 强势排行 · 龙头追踪 · 共振分析 · 明日预案 · 仅沪深主板</div>
        </div>
        <div class="hero-chip">
          <el-icon><Aim /></el-icon>
          <span>数据驱动 · 信号为王</span>
        </div>
      </div>

      <el-tabs v-model="activeTab" class="tenbagger-tabs">
        <!-- Tab 1: 异动监控 -->
        <el-tab-pane label="异动监控" name="anomalies">
          <AnomalyTabContent
            :market-temp="marketTemp"
            :anomaly-sentiment="anomalySentiment"
            v-model:active-anomaly-filter="activeAnomalyFilter"
            v-model:active-setup-track-filter="activeSetupTrackFilter"
            v-model:active-buy-point-only="activeBuyPointOnly"
            v-model:active-sort-key="activeSortKey"
            :sort-options="sortOptions"
            :current-sort-label="currentSortLabel"
            :rows="anomalyRows"
            :loading="anomalyLoading"
            :error="anomalyError"
            @retry="loadAnomalies()"
            :snapshot-time="anomalySnapshotTime"
            :snapshot-meta="anomalySnapshotMeta"
            :capital-activity="capitalActivity"
            :observation-only="anomalyObservationOnly"
            :total="anomalyTotal"
            v-model:page="anomalyPage"
            :page-size="anomalyPageSize"
            :push-buy-point-loading="pushBuyPointLoading"
            @row-click="goStock"
            @export="handleAnomalyExport"
            @push-buy-points="handlePushBuyPoints"
          />
        </el-tab-pane>

        <!-- Tab 2: 强势排行 -->
        <el-tab-pane label="强势排行" name="rank" lazy>
          <RankTabContent
            :rows="rankList"
            :mode="rankMode"
            :profile="rankProfile"
            :loading="rankLoading"
            :current-page="rankPage"
            :page-size="rankPageSize"
            :total="rankTotal"
            @update:mode="handleRankModeChange"
            @update:profile="handleRankProfileChange"
            @update:page="handleRankPageChange"
          />
        </el-tab-pane>

        <!-- Tab 3: 龙头追踪 -->
        <el-tab-pane label="龙头追踪" name="dragon" lazy>
          <DragonTabContent :rows="dragons" />
        </el-tab-pane>

        <!-- Tab 4: 共振分析 -->
        <el-tab-pane label="共振分析" name="resonance" lazy>
          <ResonanceTabContent :rows="resonanceList" />
        </el-tab-pane>

        <!-- Tab 5: 明日预案 -->
        <el-tab-pane label="明日预案" name="plan" lazy>
          <PlanTabContent
            :rows="plans"
            :pattern-rows="planPatternPool"
            :loading="planLoading"
            :error="planError"
            :snapshot-time="planSnapshotTime"
            :snapshot-stale="planSnapshotStale"
            :latest-replay="planLatestReplay"
            :market-breadth="planMarketBreadth"
            @refresh="loadPlans(true)"
          />
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import {
  Aim
} from '@element-plus/icons-vue'
import { getTenbaggerAnomalies, getTenbaggerRank, getDragonHead, getTenbaggerResonance, getTenbaggerNextDayPlan, replayTenbaggerAnomalies } from '@/api'
import { useWebSocket } from '@/composables/useWebSocket'
import { notifyError, notifySuccess, notifyWarning } from '@/utils/message'

const router = useRouter()
const AnomalyTabContent = defineAsyncComponent(() => import('./components/AnomalyTabContent.vue'))
const RankTabContent = defineAsyncComponent(() => import('./components/RankTabContent.vue'))
const DragonTabContent = defineAsyncComponent(() => import('./components/DragonTabContent.vue'))
const ResonanceTabContent = defineAsyncComponent(() => import('./components/ResonanceTabContent.vue'))
const PlanTabContent = defineAsyncComponent(() => import('./components/PlanTabContent.vue'))
const activeTab = ref('anomalies')
const rankMode = ref('bull')
const rankProfile = ref('')
const rankLoading = ref(false)
const rankPage = ref(1)
const rankPageSize = ref(20)
const rankTotal = ref(0)

const anomalyLoading = ref(false)
const anomalyError = ref('')
let disposed = false
const anomalyRequests = new Map()
let latestAnomalyRequest = null
const anomalyRows = ref([])
const anomalyTotal = ref(0)
const anomalyPage = ref(1)
const anomalyPageSize = ref(50)
const anomalySnapshotTime = ref('')
const anomalySnapshotMeta = ref({})
const capitalActivity = ref(null)
// Fail closed until a fresh response explicitly confirms an active detection session.
const anomalyObservationOnly = computed(() => anomalyLoading.value || !!anomalyError.value
  || anomalySnapshotMeta.value.detectionPaused !== false
  || anomalySnapshotMeta.value.observationOnly !== false
  || !['morning', 'afternoon'].includes(anomalySnapshotMeta.value.marketSession)
  || anomalySnapshotMeta.value.stale !== false)
const anomalyStockSummary = ref({})
const anomalyB1Summary = ref({})
const activeAnomalyFilter = ref('')
const activeSetupTrackFilter = ref('')
const activeBuyPointOnly = ref(false)
const activeSortKey = ref('priority')
const anomalySummary = ref({})
const anomalySentiment = ref({})  // V2.2融合: 市场情绪量化
const pushBuyPointLoading = ref(false)
const rankList = ref([])
const dragons = ref([])
const resonanceList = ref([])
const plans = ref([])
const planPatternPool = ref([])
const planLoading = ref(false)
const planError = ref('')
const planSnapshotTime = ref('')
const planSnapshotStale = ref(false)
const planLatestReplay = ref({})
const planMarketBreadth = ref({})
const tabLoaded = ref({
  anomalies: false,
  rank: false,
  dragon: false,
  resonance: false,
  plan: false,
})
let anomalyRefreshTimer = null
let anomalyWsRefreshTimer = null
let websocketConnectHandle = null
let unsubscribeAnomaly = null
const tabPendingLoads = {
  anomalies: null,
  rank: null,
  dragon: null,
  resonance: null,
  plan: null,
}
const { connect: connectWs, subscribe } = useWebSocket()

const isMainBoardRow = (row) => {
  const code = String(row?.code || row?.stock_code || '').trim().padStart(6, '0')
  const name = String(row?.name || row?.stock_name || '').trim().toUpperCase()
  return row?.is_tradeable !== false
    && /^(600|601|603|605|000|001|002|003)\d{3}$/.test(code)
    && !/^(ST|\*ST|退)/.test(name)
}

const mainBoardOnly = (rows) => (Array.isArray(rows) ? rows.filter(isMainBoardRow) : [])

const isPlanMainBoardRow = (row) => {
  const code = String(row?.code || row?.stock_code || '').trim().padStart(6, '0')
  const name = String(row?.name || row?.stock_name || '').trim().toUpperCase()
  if (!/^(600|601|603|605|000|001|002|003)\d{3}$/.test(code) || /^退/.test(name)) return false
  if (/^(ST|\*ST)/.test(name)) {
    return row?.research_only === true && row?.is_tradeable === false
  }
  return row?.is_tradeable !== false
}

const planMainBoardOnly = (rows) => (Array.isArray(rows) ? rows.filter(isPlanMainBoardRow) : [])

// 温度计
const marketTemp = computed(() => ({
  anomaly_total: anomalyStockSummary.value.total ?? anomalySummary.value.total ?? 0,
  limit_up: anomalyStockSummary.value.limit_up_count ?? anomalySummary.value.limit_up_count ?? 0,
  capital_anomaly: anomalyStockSummary.value.capital_count ?? anomalySummary.value.capital_count ?? 0,
  rapid_rise: anomalyStockSummary.value.rapid_rise_count ?? anomalySummary.value.rapid_rise_count ?? 0,
  breakthrough: anomalyStockSummary.value.breakthrough_count ?? anomalySummary.value.breakthrough_count ?? 0,
  buy_point: anomalyStockSummary.value.buy_point_count ?? anomalyB1Summary.value.pushable ?? 0,
}))

const sortOptions = [
  { label: '综合优先级', value: 'priority' },
  { label: '买点优先', value: 'buy_point' },
  { label: '执行等级', value: 'grade' },
  { label: '涨幅优先', value: 'change_pct' },
  { label: '资金净额', value: 'net_inflow' },
  { label: '最新时间', value: 'latest' },
]

const currentSortLabel = computed(() => (
  sortOptions.find(option => option.value === activeSortKey.value)?.label || '综合优先级'
))

const goStock = (row) => router.push(`/stocks/${row.code}`)

// 异动监控导出
const ANOMALY_COLUMNS = [
  { key: 'code', label: '代码' },
  { key: 'name', label: '名称' },
  { key: 'event_types', label: '异动类型', format: v => Array.isArray(v) ? v.join('/') : (v || '--') },
  { key: 'display_score', label: '综合评分', format: v => v?.toFixed(1) },
  { key: 'priority_score', label: '优先级评分', format: v => v?.toFixed(1) },
  { key: 'setup_grade_display', label: '评级' },
  { key: 'buy_point_type', label: '买点状态' },
  { key: 'primary_reason', label: '主要原因' },
  { key: 'secondary_reason', label: '次要原因' },
  { key: 'driver_primary', label: '主驱动板块' },
  { key: 'latest_as_of', label: '时间' },
]

const B1_COLUMNS = [
  { key: 'name', label: '名称' },
  { key: 'current_price', label: '现价', format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'b1_j', label: 'J', format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'b1_rsi', label: 'RSI', format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'b1_short_score', label: '短期分数', format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'b1_long_score', label: '长期分数', format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'day_change_pct', label: '当日涨幅', format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'change_pct_5d', label: '5日涨幅', format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'close_price', label: '收盘价', format: v => v != null ? Number(v).toFixed(2) : '--' },
  { key: 'turnover_rate', label: '换手率', format: v => v != null ? `${Number(v).toFixed(2)}%` : '--' },
  { key: 'main_net_inflow', label: '主力净额', format: v => v != null ? `${Number(v / 1e8).toFixed(2)}亿` : '--' },
  { key: 'industry', label: '行业' },
  { key: 'sub_industry', label: '行业细分' },
  { key: 'b1_kdj_signal', label: 'KDJ信号', format: v => ({ golden_cross: '金叉', death_cross: '死叉' }[v] || '--') },
  { key: 'b1_signal', label: 'B1信号' },
  { key: 'b1_signal_status_label', label: '信号状态' },
  { key: 'b1_hold_score', label: '持股分数', format: v => v != null ? `${v}` : '--' },
  { key: 'b1_success_rate_3d', label: '3日修复率(主)', format: v => v != null ? `${(Number(v) * 100).toFixed(0)}%` : '--' },
  { key: 'b1_success_rate_5d', label: '5日修复率(参考)', format: v => v != null ? `${(Number(v) * 100).toFixed(0)}%` : '--' },
  { key: 'b1_break_trend', label: '是否破趋势', format: v => v ? '是' : '否' },
  { key: 'b1_success_samples', label: '历史样本' },
]

const loadExportTool = async () => import('@/utils/export')

const handleAnomalyExport = async (command) => {
  const dateStr = new Date().toISOString().slice(0, 10)
  const filename = `异动监控_${dateStr}`
  const { exportToExcel, exportToCSV, exportWorkbookToExcel } = await loadExportTool()
  if (command === 'csv') {
    await exportToCSV(anomalyRows.value, ANOMALY_COLUMNS, filename)
    notifySuccess(`已导出 ${anomalyRows.value.length} 条数据`)
    return
  }

  const b1Rows = anomalyRows.value
    .filter(row => row.b1_signal)
    .map(row => ({
      ...row,
      name: row.name || row.code,
    }))

  await exportWorkbookToExcel([
    { sheetName: '异动监控', data: anomalyRows.value, columns: ANOMALY_COLUMNS },
    { sheetName: 'B1买点', data: b1Rows, columns: B1_COLUMNS },
  ], filename)
  notifySuccess(`已导出 ${anomalyRows.value.length} 条数据`)
}

// 同一轮筛选/页码变化只产生一个查询；全部筛选仍由原后端执行。
const anomalyParams = computed(() => ({
  min_score: 50,
  view: 'stock',
  event_type: activeAnomalyFilter.value || undefined,
  setup_track: activeSetupTrackFilter.value || undefined,
  buy_point_only: activeBuyPointOnly.value || undefined,
  sort_by: activeSortKey.value,
  page: anomalyPage.value,
  page_size: anomalyPageSize.value,
}))
const anomalyQueryKey = computed(() => JSON.stringify(anomalyParams.value))

const loadAnomalies = (refresh = false) => {
  if (disposed) return Promise.resolve(false)
  const key = anomalyQueryKey.value
  let request = anomalyRequests.get(key)
  if (request) {
    latestAnomalyRequest = request
    if (refresh) request.refreshPending = true
    anomalyLoading.value = true
    anomalyError.value = ''
    return request.promise
  }

  request = { refreshPending: false, promise: null }
  anomalyRequests.set(key, request)
  latestAnomalyRequest = request
  anomalyLoading.value = true
  anomalyError.value = ''
  const isCurrent = () => !disposed
    && latestAnomalyRequest === request && anomalyQueryKey.value === key
  request.promise = (async () => {
    try {
      const res = await getTenbaggerAnomalies(anomalyParams.value)
      if (!isCurrent()) return false
      anomalyRows.value = mainBoardOnly(res.rows)
      anomalyTotal.value = res.total || 0
      anomalySummary.value = res.summary || {}
      anomalyStockSummary.value = res.stock_summary || {}
      anomalyB1Summary.value = res.b1_summary || {}
      anomalySnapshotTime.value = res.snapshot_time || ''
      anomalySnapshotMeta.value = {
        tradeDate: res.snapshot_trade_date,
        ageSeconds: res.snapshot_age_seconds,
        stale: res.snapshot_stale,
        cachePolicy: res.snapshot_cache_policy,
        marketSession: res.market_session,
        detectionPaused: res.detection_paused,
        observationOnly: res.monitor_observation_only,
      }
      capitalActivity.value = res.capital_activity || null
      anomalySentiment.value = res.sentiment || {}
      tabLoaded.value.anomalies = true
      return true
    } catch {
      if (isCurrent()) anomalyError.value = '异动数据加载失败，请重试；已有数据保留，可能不是当前筛选的最新结果。'
      return false
    } finally {
      anomalyRequests.delete(key)
      if (isCurrent()) {
        anomalyLoading.value = false
        // 在途通知只补一次刷新，避免丢失请求开始之后的新快照。
        if (request.refreshPending && activeTab.value === 'anomalies') {
          void loadAnomalies()
        }
      }
    }
  })()
  return request.promise
}

const handlePushBuyPoints = async () => {
  if (pushBuyPointLoading.value || anomalyObservationOnly.value) return
  pushBuyPointLoading.value = true
  try {
    const res = await replayTenbaggerAnomalies({
      limit: 12,
      min_score: 50,
      event_type: activeAnomalyFilter.value || undefined,
      allow_limit_up: false,
      force_refresh: true,
      bypass_throttle: false,
      dry_run: false,
    })
    const sent = Number(res.sent || 0)
    const requested = Number(res.requested || 0)
    if (sent > 0) {
      notifySuccess(`已推送 ${sent} 只到达买点的股票`)
    } else if (requested > 0) {
      notifyWarning(`检测到 ${requested} 只买点候选，但被限频或渠道拦截`)
    } else {
      notifyWarning('当前筛选下暂无到达买点的股票')
    }
    await loadAnomalies(true)
  } catch (error) {
    notifyError('买点推送失败，请稍后重试')
  } finally {
    pushBuyPointLoading.value = false
  }
}

const loadRank = async () => {
  rankLoading.value = true
  try {
    const res = await getTenbaggerRank({
      mode: rankMode.value,
      profile: rankProfile.value || undefined,
      page: rankPage.value,
      page_size: rankPageSize.value,
    })
    rankList.value = mainBoardOnly(res.rank)
    rankTotal.value = res.total || rankList.value.length
  } catch { /* ignore */ }
  finally {
    rankLoading.value = false
  }
}

const loadDragons = async () => {
  try {
    const res = await getDragonHead({ force_refresh: true })
    dragons.value = mainBoardOnly(res.dragons)
  } catch { /* ignore */ }
}

const loadResonance = async () => {
  try {
    const res = await getTenbaggerResonance({ code: '' })
    resonanceList.value = mainBoardOnly(res.resonance).filter(r => r.best_resonance_score > 0)
  } catch { /* ignore */ }
}

const loadPlans = async (forceRefresh = false) => {
  if (planLoading.value) return false
  planLoading.value = true
  planError.value = ''
  try {
    // 页面优先读取同交易日的兼容快照；手动刷新时才触发全市场重算。
    const res = await getTenbaggerNextDayPlan({ limit: 20, force_refresh: forceRefresh })
    plans.value = planMainBoardOnly(res.plans)
    planPatternPool.value = mainBoardOnly(res.pattern_pool)
    planSnapshotTime.value = res.snapshot_time || ''
    planSnapshotStale.value = Boolean(res.snapshot_stale)
    planLatestReplay.value = res.latest_replay || {}
    planMarketBreadth.value = res.market_breadth || {}
    return true
  } catch (error) {
    planError.value = error?.message || '明日预案加载失败，请稍后重试'
    return false
  } finally {
    planLoading.value = false
  }
}

async function handleRankModeChange(value) {
  rankMode.value = value
  if (value !== 'bull') rankProfile.value = ''
  rankPage.value = 1
  await loadRank()
}

async function handleRankProfileChange(value) {
  rankProfile.value = value
  rankPage.value = 1
  await loadRank()
}

async function handleRankPageChange(page) {
  rankPage.value = page
  await loadRank()
}

function applyAnomalyPush() {
  if (disposed) return
  // 摘要与表格一起从同一响应提交，避免推送摘要与旧列表混用。
  if (activeTab.value === 'anomalies') {
    if (anomalyWsRefreshTimer) {
      window.clearTimeout(anomalyWsRefreshTimer)
    }
    anomalyWsRefreshTimer = window.setTimeout(() => {
      anomalyWsRefreshTimer = null
      if (activeTab.value === 'anomalies') void loadAnomalies(true)
    }, 250)
  }
}

function handleAnomalyPageChange(page) {
  anomalyPage.value = page
}

const tabLoaders = {
  anomalies: loadAnomalies,
  rank: loadRank,
  dragon: loadDragons,
  resonance: loadResonance,
  plan: loadPlans,
}

async function ensureTabLoaded(tab, force = false) {
  const loader = tabLoaders[tab]
  if (!loader) return
  if (!force && tabPendingLoads[tab]) {
    await tabPendingLoads[tab]
    return
  }
  if (!force && tabLoaded.value[tab]) return

  const task = Promise.resolve(loader())
    .then((loaded) => {
      if (!disposed && tab !== 'anomalies' && loaded !== false) tabLoaded.value[tab] = true
    })
    .finally(() => {
      if (tabPendingLoads[tab] === task) {
        tabPendingLoads[tab] = null
      }
    })

  tabPendingLoads[tab] = task
  await task
}

function stopAnomalyRefresh() {
  if (anomalyRefreshTimer) {
    window.clearInterval(anomalyRefreshTimer)
    anomalyRefreshTimer = null
  }
}

function startAnomalyRefresh() {
  stopAnomalyRefresh()
  anomalyRefreshTimer = window.setInterval(() => {
    if (activeTab.value === 'anomalies') {
      void loadAnomalies(true)
    }
  }, 30000)
}

function scheduleWebSocketConnect() {
  if (unsubscribeAnomaly || websocketConnectHandle) return
  const start = () => {
    websocketConnectHandle = null
    connectWs()
    unsubscribeAnomaly = subscribe('anomaly', applyAnomalyPush)
  }
  if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
    websocketConnectHandle = window.requestIdleCallback(start, { timeout: 1200 })
  } else {
    websocketConnectHandle = window.setTimeout(start, 180)
  }
}

watch(activeTab, async (tab) => {
  await ensureTabLoaded(tab)
  if (disposed || activeTab.value !== tab) return
  if (tab === 'anomalies') {
    scheduleWebSocketConnect()
    startAnomalyRefresh()
  } else {
    stopAnomalyRefresh()
  }
})

watch([activeSetupTrackFilter, activeAnomalyFilter, activeBuyPointOnly, activeSortKey], () => {
  anomalyPage.value = 1
}, { flush: 'sync' })

watch(anomalyQueryKey, () => {
  // 首轮尚未成功时也必须接收用户的最后一次选择。
  if (activeTab.value === 'anomalies') void loadAnomalies()
}, { flush: 'post' })

onMounted(async () => {
  await ensureTabLoaded(activeTab.value)
  if (disposed) return
  if (activeTab.value === 'anomalies') {
    scheduleWebSocketConnect()
    startAnomalyRefresh()
  }
})

onUnmounted(() => {
  disposed = true
  latestAnomalyRequest = null
  anomalyRequests.clear()
  stopAnomalyRefresh()
  if (websocketConnectHandle) {
    if (typeof window !== 'undefined' && typeof window.cancelIdleCallback === 'function') {
      window.cancelIdleCallback(websocketConnectHandle)
    } else {
      window.clearTimeout(websocketConnectHandle)
    }
    websocketConnectHandle = null
  }
  if (anomalyWsRefreshTimer) {
    window.clearTimeout(anomalyWsRefreshTimer)
    anomalyWsRefreshTimer = null
  }
  if (unsubscribeAnomaly) {
    unsubscribeAnomaly()
    unsubscribeAnomaly = null
  }
})
</script>

<style scoped lang="scss">
.tenbagger-page {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
}

.title-icon {
  margin-right: var(--spacing-2);
}

/* ===== hero-chip ===== */
.hero-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 16px;
  border-radius: 20px;
  background: var(--primary-50);
  border: 1px solid var(--primary-200);
  color: var(--primary-700);
  font-size: 0.8125rem;
  font-weight: 500;
}

/* ===== Tabs样式（对齐sectors subtabs-shell）===== */
.tenbagger-tabs {
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: var(--spacing-1) 0 0;
  box-shadow: none;

  :deep(.el-tabs__header) {
    margin: 0 0 var(--spacing-2);
  }

  :deep(.el-tabs__nav-wrap::after) {
    height: 1px;
  }

  :deep(.el-tabs__item) {
    height: 38px;
    line-height: 38px;
    padding: 0 14px;
    margin-right: var(--spacing-1);
    font-weight: 600;
  }

  :deep(.el-tabs__content) {
    padding-top: var(--spacing-2);
  }
}

/* ===== 响应式（对齐sectors）===== */
@media (max-width: 767px) {
  .tenbagger-page {
    gap: var(--spacing-3);
  }

  .tenbagger-tabs {
    :deep(.el-tabs__header) {
      margin-bottom: var(--spacing-2);
    }

    :deep(.el-tabs__item) {
      height: 34px;
      line-height: 34px;
      padding: 0 10px;
      margin-right: 0;
    }

    :deep(.el-tabs__content) {
      padding-top: var(--spacing-1);
    }
  }
}
</style>
