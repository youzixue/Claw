import axios from 'axios'
import { ElMessage } from 'element-plus'

const api = axios.create({
  baseURL: '/api/v1',
  timeout: 30000,
  headers: { 'Content-Type': 'application/json' },
})

// 响应拦截
api.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const msg = err.response?.data?.detail || err.message || '请求失败'
    ElMessage.error(msg)
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

// ===== 个股详情 =====
export const getStockProfile = (code) => api.get(`/stocks/${code}/profile`)
export const getStockFactors = (code) => api.get(`/stocks/${code}/factors`)
export const getStockNews = (code, params) => api.get(`/stocks/${code}/news`, { params })
export const getStockFundFlow = (code, params) => api.get(`/stocks/${code}/fund-flow`, { params })

// ===== 牛股雷达 =====
export const getTenbaggerScanner = (params) => api.get('/tenbagger/scanner', { params })
export const getTenbaggerRank = (params) => api.get('/tenbagger/rank', { params })
export const getDragonHead = (params) => api.get('/tenbagger/dragon', { params })
export const getChipAnalysis = (params) => api.get('/tenbagger/chip', { params })
export const getTenbaggerScore = (code) => api.get(`/tenbagger/${code}/score`)
export const getBreakthroughCheck = (code) => api.get(`/tenbagger/${code}/breakthrough`)

// ===== 晋级预测 =====
export const getPromotionLadder = () => api.get('/promotion/ladder')
export const getBoardHeight = () => api.get('/promotion/board-height')
export const getPromotionProbability = (code) => api.get(`/promotion/${code}/probability`)

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
export const createBackfill = (data) => api.post('/governance/backfill', data)
export const getBackfillTasks = () => api.get('/governance/backfill/tasks')
export const getStockTag = (code) => api.get(`/governance/stock-tags/${code}`)
export const getStockTagStats = () => api.get('/governance/stock-tags')

// ===== 绩效中心 =====
export const getSignalStats = () => api.get('/performance/signal-stats')
export const getFactorEval = () => api.get('/performance/factor-eval')
export const getSignalAttribution = (id) => api.get(`/performance/attribution/${id}`)

// ===== 模拟盘 =====
export const getPaperAccount = () => api.get('/paper/account')
export const getPaperPositions = () => api.get('/paper/positions')
export const paperBuy = (data) => api.post('/paper/buy', data)
export const paperSell = (data) => api.post('/paper/sell', data)
export const getPaperNav = () => api.get('/paper/nav')
export const getPaperTrades = (params) => api.get('/paper/trades', { params })

// ===== 因子引擎 =====
export const getFactorSummary = () => api.get('/factors/summary')
export const getFactorCategories = () => api.get('/factors/categories')
export const getFactorInfo = (name) => api.get(`/factors/${name}/info`)
export const evaluateFactors = (params) => api.get('/factors/evaluate', { params })
export const evaluateSingleFactor = (name, params) => api.get(`/factors/evaluate/${name}`, { params })
export const getFactorReport = () => api.get('/factors/report')
export const computeFactors = (code, params) => api.post(`/factors/compute/${code}`, null, { params })

// ===== 回测引擎 =====
export const backtestSignals = (data) => api.post('/backtest/signal', data)
export const backtestStrategy = (data) => api.post('/backtest/strategy', data)
export const getBacktestPerformance = (id) => api.get(`/backtest/performance/${id}`)
export const getBacktestTrades = (id, params) => api.get(`/backtest/trades/${id}`, { params })

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
export const getAIStatus = () => api.get('/ai/status')
export const aiSentiment = (params) => api.post('/ai/sentiment', null, { params })
export const aiEvents = (params) => api.post('/ai/events', null, { params })
export const aiSummary = (params) => api.post('/ai/summary', null, { params })

export default api
