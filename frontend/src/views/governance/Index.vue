<template>
  <div class="page-container">
    <div class="page-shell governance-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title"><el-icon class="title-icon"><Tools /></el-icon>数据治理</h2>
        <div class="page-subtitle">统一查看交易日历、数据源健康、回填任务、标记状态与推送管理</div>
      </div>
      <div class="hero-chip">
        <el-icon><Tools /></el-icon>
        <span>数据运维控制台</span>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="交易日历" name="calendar">
        <div class="metrics-panel governance-metrics-panel">
          <div class="stat-row">
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><Calendar /></el-icon><span>今日日期</span></div><div class="stat-value">{{ calendarData.date || '--' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><Flag /></el-icon><span>是否交易日</span></div><div class="stat-value" :class="calendarData.is_trade_day ? 'text-red' : 'text-gray'">{{ calendarData.is_trade_day ? '是' : '否' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><Timer /></el-icon><span>当前时段</span></div><div class="stat-value">{{ calendarData.session || '--' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><Right /></el-icon><span>下一交易日</span></div><div class="stat-value text-yellow">{{ nextTradeDay || '--' }}</div></div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="数据源健康" name="health">
        <div class="panel-card">
          <div class="filter-row">
            <el-button size="small" @click="loadHealth">刷新</el-button>
          </div>
          <el-table :data="healthList" stripe size="small" empty-text="暂无数据" :row-class-name="healthRowClassName">
            <el-table-column prop="source" label="数据源" min-width="160" />
            <el-table-column prop="api_name" label="API" min-width="200" />
            <el-table-column prop="status" label="状态" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="row.status === 'up' ? 'success' : row.status === 'degraded' ? 'warning' : 'danger'" size="small">
                  {{ row.status === 'up' ? '正常' : row.status === 'degraded' ? '降级' : '故障' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="latency_ms" label="延迟(ms)" width="90" align="right">
              <template #default="{ row }">{{ row.latency_ms ?? '--' }}</template>
            </el-table-column>
            <el-table-column prop="completeness" label="完整度" width="100" align="center">
              <template #default="{ row }">
                <el-progress :percentage="(row.completeness || 0) * 100" :stroke-width="6" :show-text="true"
                  :color="row.completeness >= 0.95 ? '#22c55e' : row.completeness >= 0.8 ? '#f59e0b' : '#ef4444'" />
              </template>
            </el-table-column>
            <el-table-column prop="message" label="检查结果" min-width="320" show-overflow-tooltip>
              <template #default="{ row }">
                <span :class="row.status === 'down' ? 'text-red' : row.status === 'degraded' ? 'text-yellow' : 'text-gray'">
                  {{ row.message || '--' }}
                </span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="预测数据质量" name="prediction-quality">
        <div class="quality-toolbar">
          <div>
            <el-tag :type="predictionQuality.gate_passed ? 'success' : 'danger'" effect="dark">
              {{ predictionQuality.gate_passed ? '预测质量闸门通过' : '预测质量闸门阻断' }}
            </el-tag>
            <span class="quality-meta">交易日 {{ predictionQuality.trade_date || '--' }} · 运行 #{{ predictionQuality.run_id || '--' }}</span>
          </div>
          <el-button type="primary" size="small" :loading="qualityRunning" @click="runQualityAudit">重新审计</el-button>
        </div>
        <div class="metrics-panel governance-metrics-panel">
          <div class="stat-row">
            <div class="stat-card gov-stat-card"><div class="metric-head"><span>阻断项</span></div><div class="stat-value text-red">{{ predictionQuality.blocking_count ?? '--' }}</div></div>
            <div class="stat-card gov-stat-card"><div class="metric-head"><span>全部问题</span></div><div class="stat-value text-yellow">{{ predictionQuality.issue_count ?? '--' }}</div></div>
            <div class="stat-card gov-stat-card"><div class="metric-head"><span>审计状态</span></div><div class="stat-value">{{ predictionQuality.status || '--' }}</div></div>
            <div class="stat-card gov-stat-card"><div class="metric-head"><span>快照阶段</span></div><div class="stat-value">{{ predictionQuality.snapshot_context || '--' }}</div></div>
          </div>
        </div>
        <div class="panel-card quality-panel">
          <div class="panel-title">数据水位</div>
          <el-table :data="predictionQuality.watermarks || []" stripe size="small" empty-text="暂无水位数据">
            <el-table-column prop="dataset" label="数据集" min-width="150" />
            <el-table-column prop="trade_date" label="数据日期" width="110" />
            <el-table-column prop="status" label="状态" width="90" align="center">
              <template #default="{ row }"><el-tag size="small" :type="qualityStatusTag(row.status)">{{ row.status }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="record_count" label="记录数" width="100" align="right" />
            <el-table-column prop="expected_count" label="期望" width="100" align="right"><template #default="{ row }">{{ row.expected_count ?? '--' }}</template></el-table-column>
            <el-table-column label="完整度" min-width="160"><template #default="{ row }"><el-progress :percentage="Math.round((row.completeness || 0) * 100)" :stroke-width="6" /></template></el-table-column>
            <el-table-column prop="max_available_at" label="最新可用时间" min-width="180"><template #default="{ row }">{{ row.max_available_at || '--' }}</template></el-table-column>
          </el-table>
        </div>
        <div class="panel-card quality-panel">
          <div class="panel-title">审计问题</div>
          <el-table :data="predictionQuality.issues || []" stripe size="small" empty-text="未发现问题">
            <el-table-column prop="severity" label="级别" width="90" align="center">
              <template #default="{ row }"><el-tag size="small" :type="row.blocking ? 'danger' : 'warning'">{{ row.severity }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="dataset" label="数据集" width="170" />
            <el-table-column prop="issue_type" label="问题类型" min-width="210" />
            <el-table-column prop="trade_date" label="日期" width="110"><template #default="{ row }">{{ row.trade_date || '--' }}</template></el-table-column>
            <el-table-column prop="message" label="说明" min-width="360" show-overflow-tooltip />
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="数据回填" name="backfill">
        <div class="panel-card" style="margin-bottom:16px">
          <div class="panel-title"><el-icon><RefreshRight /></el-icon>创建回填任务</div>
          <el-form :model="backfillForm" label-width="80px" size="small" inline>
            <el-form-item label="类型">
              <el-select v-model="backfillForm.data_type" style="width:120px">
                <el-option label="行情" value="quote" /><el-option label="资金流" value="fund_flow" />
                <el-option label="板块" value="sector" /><el-option label="新闻" value="news" />
              </el-select>
            </el-form-item>
            <el-form-item label="开始"><el-input v-model="backfillForm.start_date" placeholder="YYYY-MM-DD" style="width:130px" /></el-form-item>
            <el-form-item label="结束"><el-input v-model="backfillForm.end_date" placeholder="YYYY-MM-DD" style="width:130px" /></el-form-item>
            <el-form-item><el-button type="primary" @click="createBackfillTask" :loading="creating">创建</el-button></el-form-item>
          </el-form>
        </div>

        <div class="panel-card">
          <el-table :data="backfillTasks" stripe size="small" empty-text="暂无任务">
            <el-table-column prop="data_type" label="类型" width="80" />
            <el-table-column prop="start_date" label="开始" width="110" />
            <el-table-column prop="end_date" label="结束" width="110" />
            <el-table-column prop="status" label="状态" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="row.status === 'done' ? 'success' : row.status === 'running' ? 'warning' : row.status === 'failed' ? 'danger' : 'info'" size="small">{{ row.status }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="progress" label="进度" width="120">
              <template #default="{ row }"><el-progress :percentage="(row.progress || 0) * 100" :stroke-width="6" /></template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="股票标记" name="tags">
        <div class="metrics-panel governance-metrics-panel">
          <div class="stat-row">
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><CircleCloseFilled /></el-icon><span>不推</span></div><div class="stat-value text-red">{{ tagStats.blocked_count || '--' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><WarningFilled /></el-icon><span>停牌</span></div><div class="stat-value text-yellow">{{ tagStats.suspended_count || '--' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><View /></el-icon><span>观察</span></div><div class="stat-value text-blue">{{ tagStats.observe_count || '--' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><Top /></el-icon><span>涨停中</span></div><div class="stat-value">{{ tagStats.limit_up_count || '--' }}</div></div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="推送管理" name="push">
        <div class="panel-card push-section">
          <div class="panel-title"><el-icon><Promotion /></el-icon>推送测试</div>
          <el-button type="primary" size="small" @click="doTestPush" :loading="pushing">测试飞书推送</el-button>

          <div class="section-title" style="margin-top:16px"><el-icon><Clock /></el-icon>4时段手动推送</div>
          <div class="push-buttons">
            <el-button size="small" @click="doPushReview('morning')">盘前 8:30</el-button>
            <el-button size="small" @click="doPushReview('midday')">午盘 11:35</el-button>
            <el-button size="small" @click="doPushReview('closing')">盘后 15:10</el-button>
            <el-button size="small" @click="doPushReview('evening')">晚间 20:00</el-button>
          </div>

          <div class="section-title" style="margin-top:16px"><el-icon><DataBoard /></el-icon>推送统计</div>
          <el-descriptions v-if="pushStats" :column="2" size="small" border>
            <el-descriptions-item v-for="(val, key) in pushStats" :key="key" :label="key">{{ typeof val === 'object' ? JSON.stringify(val) : val }}</el-descriptions-item>
          </el-descriptions>
        </div>
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { getCalendarToday, getNextTradeDay, getDataSourceHealth, getPredictionDataQuality, runPredictionDataQuality, createBackfill, getBackfillTasks, getStockTagStats, testPush, pushMorningReview, pushMiddayReview, pushClosingReview, pushEveningReview, getPushStats } from '@/api'
import { notifySuccess, notifyWarning } from '@/utils/message'

const activeTab = ref('calendar')
const calendarData = ref({})
const nextTradeDay = ref('')
const healthList = ref([])
const predictionQuality = ref({})
const qualityRunning = ref(false)
const backfillTasks = ref([])
const tagStats = ref({})
const pushStats = ref(null)
const pushing = ref(false)
const creating = ref(false)

const backfillForm = ref({ data_type: 'quote', start_date: '', end_date: '' })

async function loadHealth() {
  try { const res = await getDataSourceHealth(); healthList.value = res.sources || [] } catch { /* ignore */ }
}

async function loadPredictionQuality() {
  try { predictionQuality.value = await getPredictionDataQuality() } catch { /* ignore */ }
}

async function runQualityAudit() {
  qualityRunning.value = true
  try {
    predictionQuality.value = await runPredictionDataQuality({ snapshot_context: 'manual' })
    if (predictionQuality.value.gate_passed) notifySuccess('预测数据质量审计通过')
    else notifyWarning(`发现 ${predictionQuality.value.blocking_count || 0} 个阻断问题`)
  } catch {
    notifyWarning('预测数据质量审计失败，请检查后端日志')
  } finally {
    qualityRunning.value = false
  }
}

function qualityStatusTag(status) {
  if (status === 'complete' || status === 'ok' || status === 'passed') return 'success'
  if (status === 'missing' || status === 'failed' || status === 'blocked') return 'danger'
  return 'warning'
}

function healthRowClassName({ row }) {
  if (row?.status === 'down') return 'governance-row-danger'
  if (row?.status === 'degraded') return 'governance-row-warning'
  return ''
}

async function createBackfillTask() {
  creating.value = true
  try {
    await createBackfill({ data_type: backfillForm.value.data_type, start_date: backfillForm.value.start_date, end_date: backfillForm.value.end_date })
    notifySuccess('任务已创建')
    const res = await getBackfillTasks()
    backfillTasks.value = res.tasks || []
  } catch { /* ignore */ }
  creating.value = false
}

function notifyPushResult(response, successText = '已推送') {
  const result = response?.result || response || {}
  if (result.sent) {
    notifySuccess(successText)
    return
  }
  if (result.disabled) {
    notifyWarning('推送总开关已关闭，未实际发送')
    return
  }
  if (result.throttled) {
    notifyWarning('推送被限频拦截，未实际发送')
    return
  }
  notifyWarning('推送未送达，请检查飞书/WebSocket 通道状态')
}

async function doTestPush() {
  pushing.value = true
  try {
    const res = await testPush()
    notifyPushResult(res, '飞书测试推送已发送')
    pushStats.value = await getPushStats()
  } catch { /* ignore */ }
  pushing.value = false
}

async function doPushReview(period) {
  const map = { morning: pushMorningReview, midday: pushMiddayReview, closing: pushClosingReview, evening: pushEveningReview }
  try {
    const res = await map[period]()
    notifyPushResult(res)
    pushStats.value = await getPushStats()
  } catch { /* ignore */ }
}

onMounted(async () => {
  try {
    const [c, n, h, pq, bt, ts, ps] = await Promise.allSettled([
      getCalendarToday(), getNextTradeDay(), getDataSourceHealth(), getPredictionDataQuality(), getBackfillTasks(), getStockTagStats(), getPushStats(),
    ])
    if (c.status === 'fulfilled') calendarData.value = c.value
    if (n.status === 'fulfilled') nextTradeDay.value = n.value.next_trade_day
    if (h.status === 'fulfilled') healthList.value = h.value.sources || []
    if (pq.status === 'fulfilled') predictionQuality.value = pq.value
    if (bt.status === 'fulfilled') backfillTasks.value = bt.value.tasks || []
    if (ts.status === 'fulfilled') tagStats.value = ts.value
    if (ps.status === 'fulfilled') pushStats.value = ps.value
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.governance-page { display: flex; flex-direction: column; gap: 18px; }
.governance-metrics-panel { padding: 4px; }
.stat-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 0; }
.gov-stat-card { padding: 16px; background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 14px; box-shadow: var(--claw-shadow-sm); }
.metric-head { color: var(--claw-text-muted); font-size: 13px; margin-bottom: 10px; }
.push-buttons { display: flex; gap: 8px; flex-wrap: wrap; }
.quality-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 16px; }
.quality-meta { margin-left: 12px; color: var(--claw-text-muted); font-size: 13px; }
.quality-panel { margin-top: 16px; }
.filter-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
:deep(.el-table .governance-row-danger td.el-table__cell) { background: #fff1f2; }
:deep(.el-table .governance-row-warning td.el-table__cell) { background: #fffbeb; }
:deep(.el-descriptions) { border-radius: 12px; overflow: hidden; }
@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, 1fr); }
}
</style>
