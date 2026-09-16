<template>
  <div class="page-container">
    <div class="page-shell news-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title"><el-icon class="title-icon"><Bell /></el-icon>新闻面</h2>
          <div class="page-subtitle">{{ windowLabel }}</div>
        </div>
        <div class="hero-actions">
          <div class="hero-chip">
            <el-icon><Bell /></el-icon>
            <span>{{ newsWindow.is_gap_window ? '假期/非交易日消息面' : '市场信息观察台' }}</span>
          </div>
          <el-button type="primary" @click="aiConfigVisible = true">AI 接入配置</el-button>
        </div>
      </div>

      <div class="ai-connection-bar">
        <div>
          <el-tag size="small" :type="aiStatus.ready ? 'success' : 'warning'">{{ aiStatusLabel }}</el-tag>
          <span>{{ aiStatus.auth_mode === 'openai_oauth' ? 'OpenAI 账号授权' : 'API Key 接入' }} · {{ aiStatus.active_model || aiStatus.model || '尚未读取模型' }}</span>
        </div>
        <span class="muted">{{ aiStatus.ready ? '新抓取新闻将使用当前接入分析' : 'AI 不可用时保留规则清洗，结果会明确标注' }}</span>
      </div>
      <AIConnection v-model="aiConfigVisible" @status="aiStatus = $event" />
      <el-tabs v-model="activeTab">
        <el-tab-pane label="新闻流" name="list">
          <div class="panel-card">
            <div class="panel-head cleaning-heading">
              <div>
                <strong>清洗工作台</strong>
                <div class="muted">统计范围：{{ analysisScopeLabel }} · 列表和解读仅覆盖当前返回样本</div>
              </div>
              <el-button size="small" :loading="newsLoading" :disabled="analyzeLoading" @click="loadNews(false)">刷新状态</el-button>
            </div>
            <el-alert v-if="surfaceError" :title="surfaceError" type="warning" :closable="false" class="surface-alert" />
            <div class="analysis-strip">
              <div class="analysis-stat">
                <span class="stat-label">已入库</span>
                <strong>{{ analysisStatus.fetched_count ?? 0 }}</strong>
              </div>
              <div class="analysis-stat analyzed">
                <span class="stat-label">AI 参与分析</span>
                <strong>{{ analysisStatus.ai_analyzed_count ?? '—' }}</strong>
              </div>
              <div class="analysis-stat">
                <span class="stat-label">规则降级</span>
                <strong>{{ analysisStatus.fallback_count ?? '—' }}</strong>
              </div>
              <div class="analysis-stat pending">
                <span class="stat-label">待处理（含失败）</span>
                <strong>{{ analysisStatus.pending_count ?? 0 }}</strong>
              </div>
              <div class="analysis-stat running" v-if="analysisStatus.analyzing_count">
                <span class="stat-label">分析中</span>
                <strong>{{ analysisStatus.analyzing_count }}</strong>
              </div>
              <el-button size="small" type="primary" :loading="analyzeLoading" :disabled="newsLoading || progressDisconnected || !analysisStatus.pending_count" @click="analyzeCurrentWindow">
                {{ analyzeButtonText }}
              </el-button>
            </div>
            <div class="cleaning-progress">
              <div class="progress-caption">
                <span>处理覆盖率 {{ cleaningProgress }}%（含规则降级及历史结果）</span>
                <span>失败待重试 {{ analysisStatus.failed_count ?? '—' }} · 历史分析 {{ analysisStatus.legacy_analyzed_count ?? '—' }}</span>
              </div>
              <el-progress :percentage="cleaningProgress" :show-text="false" :stroke-width="6" />
              <div v-if="analysisJob" class="job-status" role="status">
                <span>{{ progressDisconnected ? '进度连接中断，已锁定重复提交' : jobStatusText }}</span>
                <el-button v-if="progressDisconnected" size="small" :loading="analyzeLoading" @click="resumeProgress">恢复任务进度</el-button>
                <span>本批上限 {{ analysisJob.requested ?? '—' }} · 已处理 {{ analysisJob.processed ?? 0 }} · 失败 {{ analysisJob.failed ?? 0 }}</span>
              </div>
            </div>
            <div class="summary-panel" :class="`summary-${newsSummary.direction || 'neutral'}`">
              <div class="summary-header">
                <div class="summary-title">
                  <span class="summary-dot"></span>
                  <span>消息解读</span>
                  <el-tag size="small" effect="plain" :type="summaryTagType(newsSummary.direction)">{{ newsSummary.direction_label || '等待AI分析' }}</el-tag>
                </div>
                <div class="summary-side">
                  <div class="summary-metric">
                    <span>净分</span>
                    <strong :class="newsSummary.net_score > 0 ? 'text-red' : newsSummary.net_score < 0 ? 'text-green' : ''">
                      {{ formatSigned(newsSummary.net_score || 0) }}
                    </strong>
                  </div>
                  <div class="summary-metric">
                    <span>重大</span>
                    <strong>{{ newsSummary.major_count ?? 0 }}</strong>
                  </div>
                  <div class="summary-metric">
                    <span>利好/利空</span>
                    <strong>{{ newsSummary.bullish_count ?? 0 }}/{{ newsSummary.bearish_count ?? 0 }}</strong>
                  </div>
                </div>
              </div>
              <div class="summary-headline">{{ newsSummary.headline || '暂无可用消息总结' }}</div>
              <div class="summary-scope">基于当前样本中已处理的 {{ summarySampleCount }} 条新闻；未处理新闻不参与方向计算，规则降级仍沿用原有评分口径。仅作消息研究，不代表交易指令。</div>
              <details class="summary-details" v-if="newsSummary.interpretation?.length || newsSummary.key_points?.length">
                <summary>查看判断依据与完整性说明</summary>
                <div class="interpretation-list">
                  <div v-for="item in newsSummary.interpretation" :key="item" class="interpretation-item">{{ item }}</div>
                </div>
                <div class="summary-points" v-if="newsSummary.key_points?.length">
                  <div v-for="point in newsSummary.key_points" :key="point" class="summary-point">{{ point }}</div>
                </div>
              </details>
            </div>
            <div class="summary-focus" v-if="newsSummary.core_bullish_stocks?.length || newsSummary.bullish_themes?.length || newsSummary.top_sectors?.length || newsSummary.major_titles?.length">
              <div class="focus-block core-stocks" v-if="newsSummary.core_bullish_stocks?.length">
                <span class="focus-label">核心利好个股</span>
                <div v-for="item in newsSummary.core_bullish_stocks" :key="item.code" class="stock-impact">
                  <div class="stock-line">
                    <strong>{{ item.name || item.code }}</strong>
                    <span>{{ item.code }}</span>
                    <em>+{{ formatNumber(item.score, 2) }}</em>
                  </div>
                  <div class="stock-reason" v-for="reason in item.reasons" :key="reason.title">
                    <strong>逻辑：</strong>{{ reason.logic || reason.summary || reason.title }}
                  </div>
                </div>
              </div>
              <div class="focus-block theme-block" v-if="newsSummary.bullish_themes?.length">
                <span class="focus-label">受益主题</span>
                <div v-for="item in newsSummary.bullish_themes" :key="item.name" class="theme-impact">
                  <div class="theme-line">
                    <strong>{{ item.name }}</strong>
                    <em>+{{ formatNumber(item.score, 2) }}</em>
                  </div>
                  <div class="stock-reason">{{ item.logic || '板块相关利好，需要再结合个股基本面与盘口确认。' }}</div>
                </div>
              </div>
              <div class="focus-block" v-if="newsSummary.top_sectors?.length">
                <span class="focus-label">高频板块</span>
                <el-tag v-for="item in newsSummary.top_sectors" :key="item.name" size="small" effect="plain">{{ item.name }} {{ item.count }}</el-tag>
              </div>
              <div class="focus-block" v-if="newsSummary.major_titles?.length">
                <span class="focus-label">重大消息</span>
                <span v-for="item in newsSummary.major_titles" :key="item.title" class="focus-news">{{ item.title }}</span>
              </div>
            </div>
            <div class="filter-row">
              <el-select v-model="sourceFilter" placeholder="来源" clearable size="small" style="width:120px">
                <el-option v-for="s in sources" :key="s" :label="s" :value="s" />
              </el-select>
              <el-select v-model="sentimentFilter" placeholder="情感" clearable size="small" style="width:120px">
                <el-option label="利好" value="bullish" />
                <el-option label="利空" value="bearish" />
                <el-option label="中性" value="neutral" />
              </el-select>
              <el-select v-model="sourceLayerFilter" placeholder="源层级" clearable size="small" style="width:150px">
                <el-option v-for="item in sourceLayers" :key="item.value" :label="item.label" :value="item.value" />
              </el-select>
              <el-select v-model="cleaningFilter" placeholder="清洗状态" clearable size="small" style="width:150px">
                <el-option label="AI 参与分析" value="analyzed" />
                <el-option label="规则降级" value="fallback" />
                <el-option label="待清洗" value="raw" />
                <el-option label="清洗中" value="analyzing" />
                <el-option label="失败待重试" value="failed" />
              </el-select>
              <el-switch v-model="majorOnly" size="small" active-text="只看重大" @change="handleMajorOnlyChange" />
              <el-button size="small" :loading="analyzeLoading" :disabled="newsLoading || progressDisconnected" @click="refreshNewsSurface">抓取并清洗（最多 {{ windowAnalysisLimit }} 条）</el-button>
            </div>
            <div class="list-caption muted">当前展示 {{ filteredNews.length }} / {{ newsList.length }} 条返回样本（最多 50 条）· 清洗状态仅筛选本页样本</div>
            <el-timeline>
              <el-timeline-item v-for="n in filteredNews" :key="n.id || n.url || n.title" :timestamp="n.publish_time" placement="top">
                <el-card shadow="never" class="news-card">
                  <div class="news-header">
                    <el-tag size="small">{{ n.source }}</el-tag>
                    <el-tag size="small" type="info">{{ n.source_layer_label }}</el-tag>
                    <el-tag v-if="n.category" size="small" type="info">{{ n.category }}</el-tag>
                    <el-tag size="small" :type="cleaningTagType(n.nlp_status)">{{ cleaningLabel(n) }}</el-tag>
                    <el-tag v-if="n.is_major" size="small" type="danger">重大</el-tag>
                    <el-tag v-if="n.is_analyzed" size="small" :type="sentimentType(n.sentiment)">{{ sentimentLabel(n.sentiment) }} {{ confidencePct(n) }}</el-tag>
                    <el-tag v-if="n.is_analyzed" size="small" :type="directionTagType(n.direction_score)">方向分 {{ formatSigned(n.direction_score) }}</el-tag>
                    <el-tag v-if="n.impact?.holding_related" size="small" type="warning">影响持仓</el-tag>
                  </div>
                  <div class="news-title">{{ n.title }}</div>
                  <div class="news-content" v-if="n.summary || n.content">{{ n.summary || n.content }}</div>
                  <details class="news-details">
                    <summary>正文摘录与影响依据</summary>
                    <p v-if="n.content" class="news-excerpt">{{ n.content }}</p>
                    <div class="news-meta" v-if="n.major_reasons?.length">过滤依据：{{ n.major_reasons.join(' / ') }}</div>
                    <div class="news-meta" v-if="n.stock_attribution?.reason">{{ n.stock_attribution.reason }}</div>
                    <div class="impact-tags" v-if="n.related_codes?.length || n.related_sectors?.length">
                      <el-tag v-for="code in n.direct_related_codes" :key="`${n.id}-direct-${code}`" size="small" effect="plain">{{ code }} 直接证据</el-tag>
                      <el-tag v-for="code in n.inferred_related_codes" :key="`${n.id}-inferred-${code}`" size="small" effect="plain" type="info">{{ code }} 推断</el-tag>
                      <el-tag v-for="sector in n.related_sectors" :key="`${n.id}-${sector}`" size="small" effect="plain" type="info">{{ sector }}</el-tag>
                    </div>
                    <a v-if="safeNewsUrl(n.url)" :href="safeNewsUrl(n.url)" target="_blank" rel="noopener noreferrer" class="source-link">查看原文 ↗</a>
                  </details>
                </el-card>
              </el-timeline-item>
            </el-timeline>
            <el-empty v-if="!filteredNews.length && !newsLoading" :description="surfaceError ? '加载失败，请刷新重试' : newsList.length ? '当前样本无匹配新闻，请调整筛选' : '暂无新闻，可手动抓取并清洗'" :image-size="60" />
          </div>
        </el-tab-pane>

        <el-tab-pane label="利好利空" name="bull-bear">
          <div class="section-block bull-bear-section">
            <div class="bull-bear-grid">
              <div class="panel-card bull-side">
                <div class="section-title text-red"><el-icon><Top /></el-icon>重大利好</div>
                <el-card v-for="n in bullNews" :key="n.id || n.title" shadow="never" class="bull-card">
                  <div class="news-title">{{ n.title }}</div>
                  <div class="news-meta">置信度: {{ confidencePct(n) }}</div>
                  <div class="news-meta">方向分: {{ formatSigned(n.direction_score) }}</div>
                  <div class="news-content" v-if="n.summary">{{ n.summary }}</div>
                  <div class="news-meta" v-if="n.major_reasons?.length">过滤依据: {{ n.major_reasons.join(' / ') }}</div>
                </el-card>
                <el-empty v-if="!bullNews.length" description="暂无重大利好" :image-size="40" />
              </div>
              <div class="panel-card bear-side">
                <div class="section-title text-green"><el-icon><Bottom /></el-icon>重大利空</div>
                <el-card v-for="n in bearNews" :key="n.id || n.title" shadow="never" class="bear-card">
                  <div class="news-title">{{ n.title }}</div>
                  <div class="news-meta">置信度: {{ confidencePct(n) }}</div>
                  <div class="news-meta">方向分: {{ formatSigned(n.direction_score) }}</div>
                  <div class="news-content" v-if="n.summary">{{ n.summary }}</div>
                  <div class="news-meta" v-if="n.major_reasons?.length">过滤依据: {{ n.major_reasons.join(' / ') }}</div>
                </el-card>
                <el-empty v-if="!bearNews.length" description="暂无重大利空" :image-size="40" />
              </div>
            </div>
          </div>
        </el-tab-pane>

        <el-tab-pane label="影响映射" name="impact">
          <div class="section-block">
            <div class="panel-card">
              <div class="panel-head">
                <div class="section-title">持仓新闻影响</div>
                <el-button size="small" @click="loadImpactMap(false)">重算映射</el-button>
              </div>
              <el-table :data="impactMap.holdings" stripe size="small" empty-text="暂无持仓影响">
                <el-table-column prop="code" label="代码" width="90" />
                <el-table-column prop="name" label="名称" width="100" />
                <el-table-column prop="profit_pct" label="浮盈%" width="90" align="right">
                  <template #default="{ row }">{{ formatNumber(row.profit_pct, 2) }}</template>
                </el-table-column>
                <el-table-column prop="news_count" label="相关新闻" width="90" align="right" />
                <el-table-column prop="net_score" label="新闻净分" width="90" align="right">
                  <template #default="{ row }">
                    <span :class="row.net_score > 0 ? 'text-red' : row.net_score < 0 ? 'text-green' : ''">{{ formatNumber(row.net_score, 2) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="最新影响" min-width="240">
                  <template #default="{ row }">
                    <div class="latest-news" v-for="item in row.latest_news" :key="item.title">{{ item.title }}</div>
                    <span v-if="!row.latest_news?.length" class="muted">暂无映射新闻</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>

            <div class="impact-grid">
              <div class="panel-card">
                <div class="section-title">个股影响排行</div>
                <el-table :data="impactMap.stocks" stripe size="small" empty-text="暂无个股映射">
                  <el-table-column prop="code" label="代码" width="90" />
                  <el-table-column prop="name" label="名称" width="100" />
                  <el-table-column prop="news_count" label="新闻" width="70" align="right" />
                  <el-table-column prop="net_score" label="净分" width="80" align="right">
                    <template #default="{ row }">{{ formatNumber(row.net_score, 2) }}</template>
                  </el-table-column>
                </el-table>
              </div>
              <div class="panel-card">
                <div class="section-title">板块影响排行</div>
                <el-table :data="impactMap.sectors" stripe size="small" empty-text="暂无板块映射">
                  <el-table-column prop="sector_name" label="板块" min-width="130" />
                  <el-table-column prop="news_count" label="新闻" width="70" align="right" />
                  <el-table-column prop="net_score" label="净分" width="80" align="right">
                    <template #default="{ row }">{{ formatNumber(row.net_score, 2) }}</template>
                  </el-table-column>
                </el-table>
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
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import AIConnection from './AIConnection.vue'
import { getAIStatus } from '@/api'
import { getNewsList, getBullBearNews, getNewsEvents, getLockupCalendar, getNewsImpactMap, analyzeNewsWindow, refreshAndAnalyzeNewsWindow, getNewsAnalysisJob } from '@/api'

const activeTab = ref('list')
const newsList = ref([])
const bullNews = ref([])
const bearNews = ref([])
const events = ref([])
const lockups = ref([])
const impactMap = ref({ holdings: [], stocks: [], sectors: [] })
const newsWindow = ref({})
const newsSummary = ref({})
const analysisStatus = ref({ fetched_count: 0, analyzed_count: 0, analyzing_count: 0, pending_count: 0, analysis_rate: 0 })
const sourceFilter = ref('')
const sentimentFilter = ref('')
const sourceLayerFilter = ref('')
const majorOnly = ref(false)
const newsLoading = ref(false)
const analyzeLoading = ref(false)
const progressDisconnected = ref(false)
const analysisJob = ref(null)
const aiConfigVisible = ref(false)
const aiStatus = ref({})
const surfaceError = ref('')
const cleaningFilter = ref('')
let pageAlive = true
let newsRequest = 0
let cancelSleep = null
const aiStatusLabel = computed(() => aiStatus.value.ready ? 'AI 已配置' : aiStatus.value.enabled ? 'AI 待连接' : 'AI 未启用 / 状态未就绪')
const analysisScopeLabel = computed(() => `${newsWindow.value.since ? windowLabel.value : '全部历史入库新闻'}${sourceFilter.value ? ' · 来源 ' + sourceFilter.value : ''}`)
const cleaningProgress = computed(() => {
  const rate = Number(analysisStatus.value.analysis_rate)
  return Number.isFinite(rate) ? Math.min(100, Math.max(0, Math.round(rate * 1000) / 10)) : 0
})
const summarySampleCount = computed(() => newsList.value.filter(item => item.is_analyzed).length)
const jobStatusText = computed(() => ({
  queued: '任务排队中', running: '正在清洗', completed: '本批已结束，不代表历史新闻全部完成',
  failed: '任务失败，请检查配置或稍后重试', busy: '数据库忙，请稍后重试',
  cancelled: '任务已取消', not_found: '任务已失效（服务可能已重启）',
}[analysisJob.value?.status] || '等待任务状态'))

const sources = ['cls', 'em', 'cninfo', 'ths', 'sina', 'global']
const sourceLayers = [
  { value: 'domestic_news', label: '国内市场新闻' },
  { value: 'disclosure', label: '公告披露' },
  { value: 'global_market', label: '全球市场异动' },
]

const windowLabel = computed(() => {
  if (newsWindow.value?.label) return newsWindow.value.label
  return '新闻入库缓存后统一 AI/NLP 分析，并映射到股票、板块与持仓'
})

const analyzeButtonText = computed(() => {
  if (!analysisJob.value || !analyzeLoading.value) return `分析待处理新闻（最多 ${windowAnalysisLimit.value} 条）`
  const processed = analysisJob.value.processed ?? 0
  const requested = analysisJob.value.requested ?? 50
  return `分析中 ${processed}/${requested}`
})

const isGapNewsWindow = computed(() => Boolean(newsWindow.value?.is_gap_window || newsWindow.value?.since))
const windowFetchLimit = computed(() => isGapNewsWindow.value ? 200 : 80)
const windowAnalysisLimit = computed(() => isGapNewsWindow.value ? 300 : 100)
const windowBatchSize = computed(() => isGapNewsWindow.value ? 30 : 15)
const windowAnalysisConcurrency = computed(() => isGapNewsWindow.value ? 3 : 2)

const filteredNews = computed(() => {
  return newsList.value.filter(n => {
    if (cleaningFilter.value && n.nlp_status !== cleaningFilter.value) return false
    if (sourceFilter.value && n.source !== sourceFilter.value) return false
    if (sentimentFilter.value && n.sentiment !== sentimentFilter.value) return false
    if (sourceLayerFilter.value && n.source_layer !== sourceLayerFilter.value) return false
    if (majorOnly.value && !n.is_major) return false
    return true
  })
})

function formatNumber(value, digits = 2) {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : '--'
}

function confidencePct(n) {
  const value = Number(n?.bull_bear_confidence ?? n?.confidence ?? 0)
  return `${Math.round(value * 100)}%`
}

function formatSigned(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return '--'
  return `${numeric > 0 ? '+' : ''}${numeric.toFixed(2)}`
}

function directionTagType(value) {
  const numeric = Number(value)
  if (numeric > 0) return 'danger'
  if (numeric < 0) return 'success'
  return 'info'
}

function sentimentType(sentiment) {
  if (sentiment === 'bullish') return 'danger'
  if (sentiment === 'bearish') return 'success'
  return 'info'
}

function sentimentLabel(sentiment) {
  if (sentiment === 'bullish') return '利好'
  if (sentiment === 'bearish') return '利空'
  return '中性'
}

function summaryTagType(direction) {
  if (direction === 'bullish') return 'danger'
  if (direction === 'bearish') return 'success'
  return 'info'
}

function cleaningLabel(news) {
  return { analyzed: 'AI 参与分析', fallback: '规则降级', raw: '待清洗', analyzing: '清洗中', failed: '失败待重试' }[news.nlp_status] || (news.is_analyzed ? '历史分析' : '待清洗')
}
function cleaningTagType(status) {
  return status === 'analyzed' ? 'success' : status === 'failed' ? 'danger' : status === 'analyzing' ? 'primary' : status === 'fallback' ? 'warning' : 'info'
}
function safeNewsUrl(value) {
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : ''
  } catch { return '' }
}
function sleep(ms) {
  return new Promise(resolve => {
    const timer = setTimeout(() => { cancelSleep = null; resolve() }, ms)
    cancelSleep = () => { clearTimeout(timer); resolve() }
  })
}

async function loadNews(refresh = false) {
  const request = ++newsRequest
  try {
    newsLoading.value = true
    const res = await getNewsList({
      source: sourceFilter.value || '',
      sentiment: sentimentFilter.value || '',
      source_layer: sourceLayerFilter.value || '',
      major_only: majorOnly.value,
      limit: 50,
      refresh_items: refresh ? 0 : 20,
      raw_limit_per_source: refresh ? windowFetchLimit.value : 30,
      refresh,
    })
    if (!pageAlive || request !== newsRequest) return
    surfaceError.value = ''
    newsList.value = res.news || []
    newsSummary.value = res.summary || {}
    newsWindow.value = res.window || {}
    analysisStatus.value = res.analysis || analysisStatus.value
  } catch {
    if (pageAlive && request === newsRequest) surfaceError.value = '新闻加载失败，保留上次结果。请刷新重试。'
  } finally {
    if (request === newsRequest) newsLoading.value = false
  }
}

async function loadImpactMap(refresh = false) {
  try {
    const res = await getNewsImpactMap({ limit: 120, major_only: majorOnly.value, refresh })
    impactMap.value = res
    if (res.window) newsWindow.value = res.window
  } catch { /* ignore */ }
}

async function loadBullBear(refresh = false) {
  try {
    const res = await getBullBearNews({ major_only: true, refresh })
    bullNews.value = res.bull || []
    bearNews.value = res.bear || []
    if (res.window) newsWindow.value = res.window
  } catch { /* ignore */ }
}

async function loadEvents(refresh = false) {
  try {
    const res = await getNewsEvents({ refresh })
    events.value = res.events || []
    if (res.window) newsWindow.value = res.window
  } catch { /* ignore */ }
}

async function refreshNewsSurface() {
  if (analyzeLoading.value || progressDisconnected.value) return
  surfaceError.value = ''
  try {
    newsLoading.value = true
    analyzeLoading.value = true
    const started = await refreshAndAnalyzeNewsWindow({
      raw_limit_per_source: windowFetchLimit.value,
      limit: windowAnalysisLimit.value,
      batch_size: windowBatchSize.value,
      concurrency: windowAnalysisConcurrency.value,
    })
    analysisJob.value = started
    if (started.analysis && !sourceFilter.value) analysisStatus.value = started.analysis
    if (started.window) newsWindow.value = started.window
    await loadNews(false)
    await pollAnalysisJob({ refreshWhileRunning: true })
    await Promise.allSettled([
      loadBullBear(false),
      loadEvents(false),
      loadImpactMap(false),
    ])
  } catch {
    surfaceError.value = '抓取或清洗任务启动失败，请检查连接后重试。'
  } finally {
    newsLoading.value = false
    analyzeLoading.value = false
  }
}

async function runAnalysisJob({ limit = 100, batchSize = 15, concurrency = 2, refreshWhileRunning = true } = {}) {
  if (analyzeLoading.value || progressDisconnected.value) return
  surfaceError.value = ''
  try {
    analyzeLoading.value = true
    const started = await analyzeNewsWindow({ limit, batch_size: batchSize, concurrency })
    analysisJob.value = started
    if (started.analysis && !sourceFilter.value) analysisStatus.value = started.analysis
    if (started.window) newsWindow.value = started.window
    await pollAnalysisJob({ refreshWhileRunning })
  } catch {
    surfaceError.value = '清洗任务启动失败，请检查连接后重试。'
  } finally {
    analyzeLoading.value = false
  }
}

async function pollAnalysisJob({ refreshWhileRunning = true } = {}) {
  let failures = 0
  while (pageAlive && analysisJob.value?.job_id) {
    if (['completed', 'failed', 'busy', 'not_found', 'cancelled'].includes(analysisJob.value.status)) break
    try {
      await sleep(2000)
      if (!pageAlive) return
      const job = await getNewsAnalysisJob(analysisJob.value.job_id)
      if (!pageAlive) return
      failures = 0
      analysisJob.value = job
      if (job.analysis && !sourceFilter.value) analysisStatus.value = job.analysis
      if (job.window) newsWindow.value = job.window
      if (['completed', 'failed', 'busy', 'not_found', 'cancelled'].includes(job.status)) break
      if (refreshWhileRunning) await loadNews(false)
    } catch {
      failures++
      if (failures >= 3) {
        progressDisconnected.value = true
        surfaceError.value = '连续三次无法读取进度；后台任务可能仍在运行。请刷新状态，不要重复提交。'
        return
      }
    }
  }
  if (!pageAlive) return
  await Promise.allSettled([loadNews(false), loadBullBear(false), loadEvents(false), loadImpactMap(false)])
}

async function resumeProgress() {
  if (analyzeLoading.value) return
  progressDisconnected.value = false
  analyzeLoading.value = true
  try { await pollAnalysisJob() }
  finally { analyzeLoading.value = false }
}

async function analyzeCurrentWindow() {
  await runAnalysisJob({
    limit: windowAnalysisLimit.value,
    batchSize: windowBatchSize.value,
    concurrency: windowAnalysisConcurrency.value,
    refreshWhileRunning: true,
  })
}

async function handleMajorOnlyChange() {
  await Promise.allSettled([
    loadNews(false),
    loadBullBear(false),
    loadEvents(false),
    loadImpactMap(false),
  ])
}

watch([sourceFilter, sentimentFilter, sourceLayerFilter], () => { loadNews(false) })
onBeforeUnmount(() => { pageAlive = false; newsRequest++; cancelSleep?.() })
onMounted(async () => {
  getAIStatus().then(result => { if (pageAlive) aiStatus.value = result }).catch(() => {})
  try {
    const [n, bb, e, l] = await Promise.allSettled([loadNews(), loadBullBear(), loadEvents(), getLockupCalendar(), loadImpactMap()])
    if (l.status === 'fulfilled') lockups.value = l.value.lockup || []
    void n; void bb; void e
  } catch { /* ignore */ }
})
</script>

<style scoped lang="scss">
.hero-actions, .ai-connection-bar, .ai-connection-bar > div { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; }
.ai-connection-bar { justify-content: space-between; padding: 12px 16px; border: 1px solid var(--claw-border); border-radius: 10px; background: var(--claw-bg-card); color: var(--claw-text-secondary); font-size: 13px; }
.cleaning-heading { align-items: center; }
.cleaning-heading .muted { margin-top: 5px; }
.surface-alert { margin-bottom: 14px; }
.cleaning-progress { margin: 0 0 20px; }
.progress-caption, .job-status { display: flex; justify-content: space-between; gap: 10px; flex-wrap: wrap; font-size: 12px; line-height: 1.7; color: var(--claw-text-secondary); margin-bottom: 8px; }
.job-status { margin: 10px 0 0; padding: 10px 12px; border: 1px solid var(--claw-border); border-radius: 6px; }
.summary-scope { color: var(--claw-text-secondary); font-size: 12px; line-height: 1.7; }
.summary-details, .news-details { margin-top: 12px; }
.summary-details summary, .news-details summary { color: var(--el-color-primary); font-size: 12px; cursor: pointer; padding: 4px 0; }
.news-excerpt { font-size: 13px; color: var(--claw-text-secondary); white-space: pre-wrap; line-height: 1.8; overflow-wrap: anywhere; }
.source-link { color: var(--el-color-primary); display: inline-block; font-size: 12px; margin-top: 10px; }
.list-caption { margin-bottom: 18px; }
.news-page { display: flex; flex-direction: column; gap: 18px; }
.section-block { display: flex; flex-direction: column; gap: 12px; }
.bull-bear-section { gap: 0; }
.analysis-strip { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; flex-wrap: wrap; }
.analysis-stat { min-width: 104px; border: 1px solid var(--claw-border); background: var(--claw-bg-card); border-radius: 8px; padding: 8px 10px; }
.analysis-stat strong { display: block; margin-top: 2px; font-size: 18px; line-height: 1.1; }
.analysis-stat.analyzed strong { color: var(--el-color-primary); }
.analysis-stat.running strong { color: #60a5fa; }
.analysis-stat.pending strong { color: #f59e0b; }
.stat-label { color: var(--claw-text-muted); font-size: 12px; }
.summary-panel { border: 1px solid var(--claw-border); background: var(--claw-bg-card); border-radius: 8px; padding: 12px 14px; margin-bottom: 10px; box-shadow: 0 4px 14px rgba(15, 23, 42, 0.04); }
.summary-panel.summary-bullish { border-left: 3px solid #ef4444; }
.summary-panel.summary-bearish { border-left: 3px solid #22c55e; }
.summary-panel.summary-neutral { border-left: 3px solid #60a5fa; }
.summary-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 6px; }
.summary-main { min-width: 0; }
.summary-title { display: flex; align-items: center; gap: 8px; font-weight: 700; color: var(--claw-text-primary); }
.summary-dot { width: 8px; height: 8px; border-radius: 50%; background: #409eff; flex: 0 0 auto; }
.summary-bullish .summary-dot { background: #ef4444; }
.summary-bearish .summary-dot { background: #22c55e; }
.summary-headline { color: var(--claw-text-primary); line-height: 1.7; font-weight: 650; margin-bottom: 6px; }
.summary-body { display: block; }
.interpretation-list { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 16px; margin-top: 6px; }
.interpretation-item { color: var(--claw-text-secondary); font-size: 13px; line-height: 1.8; padding-left: 9px; border-left: 2px solid #dbeafe; }
.summary-points { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.summary-point { color: var(--claw-text-secondary); font-size: 12px; border: 1px solid var(--claw-border); background: var(--claw-bg-card); border-radius: 6px; padding: 3px 7px; }
.summary-side { display: grid; grid-template-columns: repeat(3, 86px); gap: 8px; flex: 0 0 auto; }
.summary-metric { border: 1px solid var(--claw-border); background: var(--claw-bg-card); border-radius: 7px; padding: 6px 8px; min-width: 0; }
.summary-metric span { display: block; color: #94a3b8; font-size: 12px; white-space: nowrap; }
.summary-metric strong { display: block; margin-top: 2px; font-size: 17px; line-height: 1.15; color: var(--claw-text-primary); }
.summary-focus { display: flex; flex-direction: column; gap: 8px; margin-bottom: 14px; }
.focus-block { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.focus-label { color: var(--claw-text-muted); font-size: 12px; min-width: 56px; }
.focus-news { color: var(--claw-text-secondary); font-size: 12px; border-bottom: 1px dashed var(--claw-border-light); max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.core-stocks { align-items: stretch; }
.stock-impact { min-width: 220px; max-width: 360px; border: 1px solid #e5edf6; border-radius: 8px; padding: 8px 10px; background: var(--claw-bg-card); }
.stock-line { display: flex; align-items: baseline; gap: 8px; margin-bottom: 5px; }
.stock-line strong { color: var(--claw-text-primary); }
.stock-line span { color: var(--claw-text-muted); font-size: 12px; }
.stock-line em { color: #ef4444; font-style: normal; font-weight: 700; margin-left: auto; }
.theme-block { align-items: stretch; }
.theme-impact { min-width: 240px; max-width: 420px; border: 1px solid #dbeafe; border-radius: 8px; padding: 8px 10px; background: var(--claw-bg-card); }
.theme-line { display: flex; align-items: baseline; gap: 8px; margin-bottom: 5px; }
.theme-line strong { color: var(--claw-text-primary); }
.theme-line em { color: #ef4444; font-style: normal; font-weight: 700; margin-left: auto; }
.stock-reason { color: var(--claw-text-secondary); font-size: 12px; line-height: 1.5; white-space: normal; }
.stock-reason strong { color: var(--claw-text-primary); }
.filter-row { display: flex; gap: 8px; margin-bottom: 16px; flex-wrap: wrap; }
.bull-bear-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.impact-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.bull-card, .bear-card, .news-card { border-radius: 12px; }
.bull-card { border-left: 3px solid #ef4444; margin-bottom: 8px; }
.bear-card { border-left: 3px solid #22c55e; margin-bottom: 8px; }
.news-header { display: flex; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.news-title { font-weight: 600; margin-bottom: 4px; }
.news-content { color: var(--claw-text-secondary); font-size: 13px; line-height: 1.8; overflow-wrap: anywhere; }
.news-title { line-height: 1.65; overflow-wrap: anywhere; }
.news-card > :deep(.el-card__body) { padding: 16px 18px; }
.news-meta { color: var(--claw-text-muted); font-size: 12px; margin-top: 4px; }
.section-title { display: inline-flex; align-items: center; gap: 8px; margin-bottom: 14px; }
.panel-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 12px; }
.impact-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.latest-news { color: var(--claw-text-secondary); font-size: 12px; line-height: 1.5; }
.muted { color: var(--claw-text-muted); font-size: 12px; }
.text-red { color: #ef4444; }
.text-green { color: #22c55e; }
.summary-metric strong.text-red { color: #ef4444; }
.summary-metric strong.text-green { color: #22c55e; }
:deep(.el-tabs__header) { margin-bottom: 18px; }
:deep(.el-tabs__nav-wrap::after) { background: var(--claw-border-light); }
:deep(.el-tabs__item) { height: 40px; font-weight: 500; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 14px; overflow: hidden; box-shadow: var(--claw-shadow-sm); }
:deep(.el-table th.el-table__cell) { background: #f7faff; }
@media (max-width: 768px) {
  .hero-actions { width: 100%; justify-content: space-between; }
  .analysis-stat { min-width: 0; flex: 1 1 40%; }
  .summary-side { width: 100%; }
  .theme-impact, .stock-impact { min-width: 0; width: 100%; max-width: 100%; }
  .filter-row > :deep(.el-button) { margin-left: 0; }
  .news-card > :deep(.el-card__body) { padding: 12px; }
  .bull-bear-grid { grid-template-columns: 1fr; }
  .impact-grid { grid-template-columns: 1fr; }
  .summary-header { flex-direction: column; }
  .summary-side { grid-template-columns: repeat(3, 1fr); }
  .interpretation-list { grid-template-columns: 1fr; }
}
</style>
