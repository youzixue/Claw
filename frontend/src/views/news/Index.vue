<template>
  <div class="page-container">
    <div class="page-shell news-page">
      <div class="page-hero">
      <div>
        <h2 class="page-title">📰 新闻面</h2>
        <div class="page-subtitle">统一查看新闻流、利好利空、事件提取与解禁日历</div>
      </div>
      <div class="hero-chip">
        <el-icon><Bell /></el-icon>
        <span>市场信息观察台</span>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <el-tab-pane label="新闻流" name="list">
        <div class="panel-card">
          <div class="filter-row">
            <el-select v-model="sourceFilter" placeholder="来源" clearable size="small" style="width:120px">
              <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
            </el-select>
            <el-select v-model="sentimentFilter" placeholder="情感" clearable size="small" style="width:120px">
              <el-option label="利好" value="bullish" /><el-option label="利空" value="bearish" /><el-option label="中性" value="neutral" />
            </el-select>
            <el-button size="small" @click="loadNews">刷新</el-button>
          </div>
          <el-timeline>
            <el-timeline-item v-for="n in filteredNews" :key="n.url || n.title" :timestamp="n.publish_time" placement="top">
              <el-card shadow="never" class="news-card">
                <div class="news-header">
                  <el-tag size="small">{{ n.source }}</el-tag>
                  <el-tag v-if="n.category" size="small" type="info">{{ n.category }}</el-tag>
                </div>
                <div class="news-title">{{ n.title }}</div>
                <div class="news-content" v-if="n.content">{{ n.content }}</div>
              </el-card>
            </el-timeline-item>
          </el-timeline>
          <el-empty v-if="!filteredNews.length" description="暂无新闻" :image-size="60" />
        </div>
      </el-tab-pane>

      <el-tab-pane label="利好利空" name="bull-bear">
        <div class="section-block bull-bear-section">
          <div class="bull-bear-grid">
          <div class="panel-card bull-side">
            <div class="section-title text-red"><el-icon><Top /></el-icon>🔴 利好</div>
            <el-card v-for="n in bullNews" :key="n.title" shadow="never" class="bull-card">
              <div class="news-title">{{ n.title }}</div>
              <div class="news-meta">置信度: {{ (n.bull_bear_confidence * 100).toFixed(0) }}%</div>
            </el-card>
            <el-empty v-if="!bullNews.length" description="暂无利好" :image-size="40" />
          </div>
            <div class="panel-card bear-side">
            <div class="section-title text-green"><el-icon><Bottom /></el-icon>🟢 利空</div>
            <el-card v-for="n in bearNews" :key="n.title" shadow="never" class="bear-card">
              <div class="news-title">{{ n.title }}</div>
              <div class="news-meta">置信度: {{ (n.bull_bear_confidence * 100).toFixed(0) }}%</div>
            </el-card>
            <el-empty v-if="!bearNews.length" description="暂无利空" :image-size="40" />
            </div>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="事件提取" name="events">
        <div class="panel-card">
          <el-table :data="events" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="type" label="事件类型" width="120">
              <template #default="{ row }"><el-tag size="small">{{ row.type }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="source_news" label="来源新闻" min-width="200" show-overflow-tooltip />
            <el-table-column prop="impact_scope" label="影响范围" width="100" />
          </el-table>
        </div>
      </el-tab-pane>

      <el-tab-pane label="解禁日历" name="lockup">
        <div class="panel-card">
          <el-table :data="lockups" stripe size="small" empty-text="暂无数据">
            <el-table-column prop="code" label="代码" width="80" />
            <el-table-column prop="name" label="名称" width="80" />
            <el-table-column prop="unlock_date" label="解禁日期" width="110" />
            <el-table-column prop="unlock_volume" label="解禁量(万)" width="100" align="right">
              <template #default="{ row }">{{ row.unlock_volume ? (row.unlock_volume / 1e4).toFixed(0) : '--' }}</template>
            </el-table-column>
            <el-table-column prop="unlock_ratio" label="占比%" width="80" align="right">
              <template #default="{ row }">{{ row.unlock_ratio?.toFixed(1) || '--' }}</template>
            </el-table-column>
            <el-table-column prop="risk_level" label="风险" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="row.risk_level === 'high' ? 'danger' : row.risk_level === 'medium' ? 'warning' : 'info'" size="small">{{ row.risk_level }}</el-tag>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-tab-pane>
    </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { getNewsList, getBullBearNews, getNewsEvents, getLockupCalendar } from '@/api'

const activeTab = ref('list')
const newsList = ref([])
const bullNews = ref([])
const bearNews = ref([])
const events = ref([])
const lockups = ref([])
const sourceFilter = ref('')
const sentimentFilter = ref('')

const sources = ['cls', 'em', 'cninfo', 'ths', 'sina', 'global']

const filteredNews = computed(() => {
  return newsList.value.filter(n => {
    if (sourceFilter.value && n.source !== sourceFilter.value) return false
    if (sentimentFilter.value && n.sentiment !== sentimentFilter.value) return false
    return true
  })
})

async function loadNews() {
  try {
    const res = await getNewsList({ source: sourceFilter.value || '', sentiment: sentimentFilter.value || '', limit: 50 })
    newsList.value = res.news || []
  } catch { /* ignore */ }
}

onMounted(async () => {
  try {
    const [n, bb, e, l] = await Promise.allSettled([loadNews(), getBullBearNews(), getNewsEvents(), getLockupCalendar()])
    if (bb.status === 'fulfilled') { bullNews.value = bb.value.bull || []; bearNews.value = bb.value.bear || [] }
    if (e.status === 'fulfilled') events.value = e.value.events || []
    if (l.status === 'fulfilled') lockups.value = l.value.lockup || []
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.news-page { display: flex; flex-direction: column; gap: 18px; }
.section-block { display: flex; flex-direction: column; gap: 12px; }
.bull-bear-section { gap: 0; }
.filter-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.bull-bear-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.bull-card, .bear-card, .news-card { border-radius: 12px; }
.bull-card { border-left: 3px solid #ef4444; margin-bottom: 8px; }
.bear-card { border-left: 3px solid #22c55e; margin-bottom: 8px; }
.news-header { display: flex; gap: 6px; margin-bottom: 6px; }
.news-title { font-weight: 600; margin-bottom: 4px; }
.news-content { color: var(--claw-text-secondary); font-size: 12px; line-height: 1.6; }
.news-meta { color: var(--claw-text-muted); font-size: 12px; margin-top: 4px; }
.section-title { display: inline-flex; align-items: center; gap: 8px; margin-bottom: 14px; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
@media (max-width: 768px) {
  .bull-bear-grid { grid-template-columns: 1fr; }
}
</style>
