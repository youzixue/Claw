<template>
  <div class="page-container">
    <div class="page-shell governance-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title">🔧 数据治理</h2>
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
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><Flag /></el-icon><span>是否交易日</span></div><div class="stat-value" :class="calendarData.is_trade_day ? 'text-red' : 'text-gray'">{{ calendarData.is_trade_day ? '✅ 是' : '❌ 否' }}</div></div>
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
          <el-table :data="healthList" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="source" label="数据源" width="100" />
            <el-table-column prop="api_name" label="API" min-width="200" />
            <el-table-column prop="status" label="状态" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="row.status === 'up' ? 'success' : row.status === 'degraded' ? 'warning' : 'danger'" size="small">
                  {{ row.status === 'up' ? '✅ 正常' : row.status === 'degraded' ? '⚠️ 降级' : '❌ 故障' }}
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
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><CircleCloseFilled /></el-icon><span>🚫 不推</span></div><div class="stat-value text-red">{{ tagStats.blocked_count || '--' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><WarningFilled /></el-icon><span>停牌</span></div><div class="stat-value text-yellow">{{ tagStats.suspended_count || '--' }}</div></div>
          <div class="stat-card gov-stat-card"><div class="metric-head"><el-icon><View /></el-icon><span>👁️ 观察</span></div><div class="stat-value text-blue">{{ tagStats.observe_count || '--' }}</div></div>
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
            <el-button size="small" @click="doPushReview('morning')">🌅 盘前8:30</el-button>
            <el-button size="small" @click="doPushReview('midday')">☀️ 午盘11:35</el-button>
            <el-button size="small" @click="doPushReview('closing')">🌆 盘后15:10</el-button>
            <el-button size="small" @click="doPushReview('evening')">🌙 晚间20:00</el-button>
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
import { getCalendarToday, getNextTradeDay, getDataSourceHealth, createBackfill, getBackfillTasks, getStockTagStats, testPush, pushMorningReview, pushMiddayReview, pushClosingReview, pushEveningReview, getPushStats } from '@/api'
import { ElMessage } from 'element-plus'

const activeTab = ref('calendar')
const calendarData = ref({})
const nextTradeDay = ref('')
const healthList = ref([])
const backfillTasks = ref([])
const tagStats = ref({})
const pushStats = ref(null)
const pushing = ref(false)
const creating = ref(false)

const backfillForm = ref({ data_type: 'quote', start_date: '', end_date: '' })

async function loadHealth() {
  try { const res = await getDataSourceHealth(); healthList.value = res.sources || [] } catch { /* ignore */ }
}

async function createBackfillTask() {
  creating.value = true
  try {
    await createBackfill({ data_type: backfillForm.value.data_type, start_date: backfillForm.value.start_date, end_date: backfillForm.value.end_date })
    ElMessage.success('任务已创建')
    const res = await getBackfillTasks()
    backfillTasks.value = res.tasks || []
  } catch { /* ignore */ }
  creating.value = false
}

async function doTestPush() {
  pushing.value = true
  try { await testPush(); ElMessage.success('推送已发送') } catch { /* ignore */ }
  pushing.value = false
}

async function doPushReview(period) {
  const map = { morning: pushMorningReview, midday: pushMiddayReview, closing: pushClosingReview, evening: pushEveningReview }
  try { await map[period](); ElMessage.success('已推送') } catch { /* ignore */ }
}

onMounted(async () => {
  try {
    const [c, n, h, bt, ts, ps] = await Promise.allSettled([
      getCalendarToday(), getNextTradeDay(), getDataSourceHealth(), getBackfillTasks(), getStockTagStats(), getPushStats(),
    ])
    if (c.status === 'fulfilled') calendarData.value = c.value
    if (n.status === 'fulfilled') nextTradeDay.value = n.value.next_trade_day
    if (h.status === 'fulfilled') healthList.value = h.value.sources || []
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
.filter-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
:deep(.el-descriptions) { border-radius: 12px; overflow: hidden; }
@media (max-width: 768px) {
  .stat-row { grid-template-columns: repeat(2, 1fr); }
}
</style>
