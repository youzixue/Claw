import axios from 'axios'
import { notifyError } from '@/utils/message'

function formatApiError(err) {
  const detail = err.response?.data?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    return detail
      .map((item) => item?.msg || item?.message || JSON.stringify(item))
      .filter(Boolean)
      .join('；') || err.message || '请求失败'
  }
  if (detail && typeof detail === 'object') {
    return detail.message || detail.msg || JSON.stringify(detail)
  }
  return err.message || '请求失败'
}

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 60000,
  headers: { 'Content-Type': 'application/json' },
})

// 响应拦截
api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const msg = formatApiError(err)
    if (!err.config?.silent) {
      notifyError(msg)
    }
    return Promise.reject(err)
  }
)

// ===== 行情总览 =====
export const getDashboardOverview = () => api.get('/dashboard/overview')
export const getDashboardOverviewV2 = () => api.get('/dashboard/overview-v2')

// ===== 板块营地 =====
export const getSectorStrength = (params) => api.get('/sectors/strength', { params })
export const getSectorRotation = (params) => api.get('/sectors/rotation', { params })
export const getSectorPersistence = (params) => api.get('/sectors/persistence', { params })
export const getSectorCount = () => api.get('/sectors/count')
// 板块生命周期(新增)
export const getSectorLifecycle = (params) => api.get('/sectors/lifecycle', { params })
export const getSectorLifecycleCalendar = (params) => api.get('/sectors/lifecycle/calendar', { params })
export const getSectorMainLines = (params) => api.get('/sectors/main-lines', { params })
// 板块K线
export const getSectorKline = (params) => api.get('/sectors/kline', { params })
export const getSectorKlineBatch = (params) => api.get('/sectors/kline/batch', { params })

// ===== 商品联动 =====
export const getCommodityLinkageSummary = (params) => api.get('/commodity-linkage/summary', { params })

// ===== 个股中心 / 详情 =====
export const searchStocks = (params) => api.get('/stocks/search', { params })
export const getStockProfile = (code) => api.get(`/stocks/${code}/profile`)
export const getStockFactors = (code) => api.get(`/stocks/${code}/factors`)
export const getStockNews = (code, params) => api.get(`/stocks/${code}/news`, { params })
export const getStockFundFlow = (code, params) => api.get(`/stocks/${code}/fund-flow`, { params })
export const getStockSpot = (code) => api.get(`/stocks/spot/${code}`)
export const getStockKline = (code, params) => api.get(`/stocks/kline/${code}`, { params })

// ===== 牛股雷达 =====
export const getTenbaggerAnomalies = (params) => api.get('/tenbagger/anomalies', { params })
export const replayTenbaggerAnomalies = (params) => api.post('/tenbagger/anomalies/replay', null, { params })
export const getTenbaggerRank = (params) => api.get('/tenbagger/rank', { params })
export const getDragonHead = (params) => api.get('/tenbagger/dragon', { params })
export const getTenbaggerResonance = (params) => api.get('/tenbagger/resonance', { params })
export const getTenbaggerNextDayPlan = (params) => api.get('/tenbagger/next-day-plan', { params })
export const getStockNextDayPlan = (code) => api.get(`/tenbagger/${code}/next-day-plan`)
export const getTenbaggerScore = (code) => api.get(`/tenbagger/${code}/score`)
export const getStockResonance = (code) => api.get(`/tenbagger/${code}/resonance`)
export const getBreakthroughCheck = (code) => api.get(`/tenbagger/${code}/breakthrough`)

// ===== 晋级预测 =====
export const getPromotionLadder = () => api.get('/promotion/ladder')
export const getBoardHeight = () => api.get('/promotion/board-height')
export const getPromotionCandidates = (params) => api.get('/promotion/candidates', { params })
export const getPromotionLearningReview = (params) => api.get('/promotion/learning-review', { params })
export const getPromotionProbability = (code, params) => api.get(`/promotion/${code}/probability`, { params })

// ===== 情绪面 =====
export const getSentimentCycle = () => api.get('/sentiment/cycle')
export const getSentimentStats = () => api.get('/sentiment/stats')
export const getMarginData = () => api.get('/sentiment/margin')

// ===== 竞价分析 =====
export const getAuctionSignals = (params) => api.get('/auction/signals', { params })
export const getAuctionSummary = (params) => api.get('/auction/summary', { params })
export const collectAuctionData = (params) => api.post('/auction/collect', null, { params })
export const getAuctionFactors = (code, params) => api.get(`/auction/${code}/factors`, { params })

// ===== 融资融券 =====
export const getMarginAnomalies = (params) => api.get('/margin/anomalies', { params })
export const getMarginIndex = (params) => api.get('/margin/index', { params })
export const getMarginDetail = (code, params) => api.get(`/margin/${code}/detail`, { params })
export const collectMarginData = (params) => api.post('/margin/collect', null, { params })

// ===== 新闻面 =====
export const getNewsList = (params) => api.get('/news/list', { params })
export const getBullBearNews = (params) => api.get('/news/bull-bear', { params })
export const getNewsEvents = (params) => api.get('/news/events', { params })
export const getStockNewsByCode = (code, params) => api.get(`/news/${code}`, { params })
export const getLockupCalendar = (params) => api.get('/news/lockup-calendar', { params })
export const getNewsImpactMap = (params) => api.get('/news/impact-map', { params })
export const analyzeNewsWindow = (params) => api.post('/news/analyze-window', null, { params })
export const refreshAndAnalyzeNewsWindow = (params) => api.post('/news/refresh-and-analyze', null, { params })
export const getNewsAnalysisJob = (jobId) => api.get(`/news/analysis-jobs/${jobId}`, { silent: true })

// ===== 风控中心 =====
export const getRiskRules = () => api.get('/risk/rules')
export const checkRisk = (data) => api.post('/risk/check', data)
export const toggleRiskRule = (name, enabled) => api.put(`/risk/rules/${name}/toggle`, null, { params: { enabled } })
export const getLockupCalendarRisk = (params) => api.get('/risk/lockup/calendar', { params })
export const getLockupUpcoming = (params) => api.get('/risk/lockup/upcoming', { params })
export const getLockupCheck = (code, params) => api.get(`/risk/lockup/${code}`, { params })
export const getSentimentState = (params) => api.get('/risk/sentiment/state', { params })
export const getSentimentHistory = (params) => api.get('/risk/sentiment/history', { params })

// ===== 数据治理 =====
export const getCalendarToday = () => api.get('/governance/calendar/today')
export const getNextTradeDay = () => api.get('/governance/calendar/next-trade-day')
export const getDataSourceHealth = () => api.get('/governance/health')
export const getPredictionDataQuality = (params) => api.get('/governance/prediction-quality', { params })
export const runPredictionDataQuality = (params) => api.post('/governance/prediction-quality/run', null, { params })
export const createBackfill = (data) => api.post('/governance/backfill', data)
export const getBackfillTasks = () => api.get('/governance/backfill/tasks')
export const getStockTag = (code) => api.get(`/governance/stock-tags/${code}`)
export const getStockTagStats = () => api.get('/governance/stock-tags')

// ===== 模型实验室 =====
export const getPromotionModelIdentity = () => api.get('/model-lab/identity')
export const getPromotionPredictionRuns = (params) => api.get('/model-lab/runs', { params })
export const getPromotionPredictionRun = (runId, params) => api.get(`/model-lab/runs/${runId}`, { params })
export const getPromotionModelArtifacts = () => api.get('/model-lab/artifacts')
export const getPromotionExperiments = () => api.get('/model-lab/experiments')
export const getPromotionTrainingRuns = (params) => api.get('/model-lab/training-runs', { params })
export const trainPromotionChallenger = (data) => api.post('/model-lab/train', data)
export const runPromotionShadow = (data) => api.post('/model-lab/shadow/run', data)
export const evaluatePromotionShadow = (data) => api.post('/model-lab/shadow/evaluate', data)
export const getPromotionShadowRuns = (params) => api.get('/model-lab/shadow-runs', { params })
export const getPromotionShadowRun = (shadowRunId) => api.get(`/model-lab/shadow-runs/${shadowRunId}`)
export const getPromotionShadowEvaluations = (params) => api.get('/model-lab/shadow-evaluations', { params })
export const getPromotionDeployments = () => api.get('/model-lab/deployments')
const promotionGovernanceWrite = (path, data = {}) => {
  const { governance_token: governanceToken, ...body } = data
  return api.post(path, body, {
    headers: { 'X-Claw-Governance-Token': governanceToken || '' },
  })
}
export const approvePromotionDeployment = (data) => promotionGovernanceWrite('/model-lab/deployments/approve', data)
export const rollbackPromotionDeployment = (data) => promotionGovernanceWrite('/model-lab/deployments/rollback', data)
export const getMarketRegimeIdentity = () => api.get('/market-regime/identity')
export const getCurrentMarketRegime = (params) => api.get('/market-regime/current', { params })
export const getMarketRegimeHistory = (params) => api.get('/market-regime/history', { params })
export const classifyMarketRegime = (data) => api.post('/market-regime/classify', data)

// ===== 每日复盘工作台 =====
export const buildDailyReview = (data) => api.post('/daily-review/snapshots/build', data)
export const getDailyReviewSnapshots = (params) => api.get('/daily-review/snapshots', { params })
export const getDailyReviewSnapshot = (snapshotId, params) => api.get(`/daily-review/snapshots/${snapshotId}`, { params })
export const getLatestDailyReview = (params) => api.get('/daily-review/latest', { params })
export const getDailyReviewAttributions = (params) => api.get('/daily-review/attributions', { params })
export const getDailyReviewAutomationRuns = (params) => api.get('/daily-review/automation-runs', { params })
export const getDailyReviewAlerts = (params) => api.get('/daily-review/alerts', { params })
export const replayDailyReviews = (data) => api.post('/daily-review/replay', data)
export const createDailyReviewNote = (data) => api.post('/daily-review/notes', data)
export const getDailyReviewNotes = (params) => api.get('/daily-review/notes', { params })
export const getDailyReviewGptCapabilities = () => api.get('/daily-review/gpt-reports/capabilities', { silent: true })
export const getDailyReviewGptReports = (params) => api.get('/daily-review/gpt-reports', { params })
export const generateDailyReviewGptReport = (data) => api.post('/daily-review/gpt-reports/generate', data)

// ===== 绩效中心 =====
export const getSignalStats = () => api.get('/performance/signal-stats')
export const getFactorEval = () => api.get('/performance/factor-eval')
export const getSignalAttribution = (id) => api.get(`/performance/attribution/${id}`)

// ===== 模拟盘（六个基准策略并行；B/C/D/F 另有隔离候选子账户） =====
export const getPaperAccount = (accountName = 'default') => api.get('/paper/account', { params: { account_name: accountName } })
export const getPaperPositions = (accountName = 'default') => api.get('/paper/positions', { params: { account_name: accountName } })
export const paperBuy = (data, accountName = 'default') => api.post('/paper/buy', data, { params: { account_name: accountName } })
export const paperSell = (data, accountName = 'default') => api.post('/paper/sell', data, { params: { account_name: accountName } })
export const getPaperNav = (accountName = 'default') => api.get('/paper/nav', { params: { account_name: accountName } })
export const getPaperTrades = (params, accountName = 'default') => api.get('/paper/trades', { params: { ...params, account_name: accountName } })
export const getPaperAutoStatus = (accountName = 'default') => api.get('/paper/auto/status', { params: { account_name: accountName } })
export const getPaperAutoLogs = (params, accountName = 'default') => api.get('/paper/auto/logs', { params: { ...params, account_name: accountName } })
export const getPaperAutoEvaluation = (accountName = 'default') => api.get('/paper/auto/evaluation', { params: { account_name: accountName } })
export const getPaperChallengerComparison = (params = {}) => api.get('/paper/challengers/comparison', { params })
export const getPaperExperimentReport = (accountName) => api.get('/paper/experiment/report', {
  params: accountName ? { account_name: accountName } : {},
})
export const runPaperAutoTrade = (data, accountName = 'default') => api.post('/paper/auto/run', data, { params: { account_name: accountName } })

// ===== 因子引擎 =====
export const getFactorSummary = () => api.get('/factors/summary')
export const getFactorCategories = () => api.get('/factors/categories')
export const getFactorInfo = (name) => api.get(`/factors/${name}/info`)
// 只读报告；显式研究计算复用下方 runDailyEvaluation 的 POST。
export const evaluateFactors = (params) => api.get('/factors/evaluate', { params })
export const evaluateSingleFactor = (name, params) => api.get(`/factors/evaluate/${name}`, { params })
export const getFactorReport = () => api.get('/factors/report')
export const computeFactors = (code, params) => api.post(`/factors/compute/${code}`, null, { params })

// ===== 回测引擎 =====
export const getBacktestStatus = () => api.get('/backtest/status')
export const getBacktestStrategies = () => api.get('/backtest/strategies')
export const getBacktestRuns = (params) => api.get('/backtest/runs', { params })
export const getBacktestCandidates = (params) => api.get('/backtest/candidates', { params })
export const compareBacktestRuns = (params) => api.get('/backtest/compare', { params })
export const backfillBacktestSignals = (data) => api.post('/backtest/backfill-signals', data)
export const backtestSignals = (data) => api.post('/backtest/signal', data)
export const backtestStrategy = (data) => api.post('/backtest/strategy', data)
export const getBacktestPerformance = (id) => api.get(`/backtest/performance/${id}`)
export const getBacktestTrades = (id, params) => api.get(`/backtest/trades/${id}`, { params })
export const getBacktestSignalResults = (id, params) => api.get(`/backtest/signals/${id}`, { params })

// ===== 因子评估调度 =====
export const runDailyEvaluation = (params) => api.post('/eval/evaluate/daily', null, { params })
export const computeAndStoreFactors = (params) => api.post('/eval/compute-and-store', null, { params })
export const getEvaluationReport = () => api.get('/eval/report')
export const getDecayingFactors = () => api.get('/eval/decaying')
export const getFactorWeights = () => api.get('/eval/weights')
export const forceRecompute = (params) => api.post('/eval/force-recompute', null, { params })
export const getFactorValues = (name, params) => api.get(`/eval/values/${name}`, { params })
export const getFactorHistory = (name, code, params) => api.get(`/eval/history/${name}/${code}`, { params })

// ===== 推送中心 =====
export const testPush = () => api.get('/push/test')
export const sendPush = (data) => api.post('/push/send', data)
export const getPushStats = () => api.get('/push/stats')
export const getPushHistory = (params) => api.get('/push/history', { params })
export const getThrottleStats = () => api.get('/push/throttle')
export const pushMorningReview = () => api.post('/push/review/morning')
export const pushMiddayReview = () => api.post('/push/review/midday')
export const pushClosingReview = () => api.post('/push/review/closing')
export const pushEveningReview = () => api.post('/push/review/evening')
export const pushLimitUpAlert = (params) => api.post('/push/alert/limit-up', null, { params })
export const pushCapitalAnomaly = (params) => api.post('/push/alert/capital', null, { params })
export const pushBreakthroughAlert = (params) => api.post('/push/alert/breakthrough', null, { params })

// ===== AI模块 =====
export const getAIStatus = () => api.get('/ai/status', { silent: true })
export const getAIOAuthModels = () => api.get('/ai/oauth/models', { silent: true, timeout: 30000 })
export const saveAIConfig = (config) => api.put('/ai/config', config)
export const testAIConnection = () => api.post('/ai/test', null, { timeout: 180000 })
export const startAIOAuth = () => api.post('/ai/oauth/start')
export const cancelAIOAuth = () => api.post('/ai/oauth/cancel')
export const logoutAIOAuth = () => api.post('/ai/oauth/logout')
export const aiSentiment = (params) => api.post('/ai/sentiment', null, { params })
export const aiEvents = (params) => api.post('/ai/events', null, { params })
export const aiSummary = (params) => api.post('/ai/summary', null, { params })

export default api
