import { createRouter, createWebHistory } from 'vue-router'

const loadAppLayout = () => import('@/components/layout/AppLayout.vue')
const loadDashboard = () => import('@/views/dashboard/Index.vue')
const loadSectors = () => import('@/views/sectors/Index.vue')
const loadCommodityLinkage = () => import('@/views/commodity-linkage/Index.vue')
const loadTenbagger = () => import('@/views/tenbagger/Index.vue')
const loadTenbaggerAnomalyTab = () => import('@/views/tenbagger/components/AnomalyTabContent.vue')
const loadPromotion = () => import('@/views/promotion/Index.vue')
const loadDailyReview = () => import('@/views/daily-review/Index.vue')
const loadStockCenter = () => import('@/views/stocks/Index.vue')
const loadStockDetail = () => import('@/views/stocks/Detail.vue')
const loadSentiment = () => import('@/views/sentiment/Index.vue')
const loadAuction = () => import('@/views/auction/Index.vue')
const loadMargin = () => import('@/views/margin/Index.vue')
const loadNews = () => import('@/views/news/Index.vue')
const loadRisk = () => import('@/views/risk/Index.vue')
const loadFactors = () => import('@/views/factors/Index.vue')
const loadPerformance = () => import('@/views/performance/Index.vue')
const loadPaper = () => import('@/views/paper/Index.vue')
const loadBacktest = () => import('@/views/backtest/Index.vue')
const loadModelLab = () => import('@/views/model-lab/Index.vue')
const loadGovernance = () => import('@/views/governance/Index.vue')

export function prefetchTenbaggerRoute() {
  return Promise.all([
    loadTenbagger(),
    loadTenbaggerAnomalyTab(),
  ])
}

const routes = [
  {
    path: '/',
    component: loadAppLayout,
    children: [
      { path: '', name: 'dashboard', component: loadDashboard, meta: { title: '行情总览', icon: 'TrendCharts' } },
      { path: 'stocks', name: 'stock-center', component: loadStockCenter, meta: { title: '个股中心', icon: 'Search' } },
      { path: 'sectors', name: 'sectors', component: loadSectors, meta: { title: '板块营地', icon: 'PieChart' } },
      { path: 'commodity-linkage', name: 'commodity-linkage', component: loadCommodityLinkage, meta: { title: '商品联动', icon: 'Connection' } },
      { path: 'tenbagger', name: 'tenbagger', component: loadTenbagger, meta: { title: '牛股雷达', icon: 'Aim' } },
      { path: 'promotion', name: 'promotion', component: loadPromotion, meta: { title: '晋级预测', icon: 'TopRight' } },
      { path: 'daily-review', name: 'daily-review', component: loadDailyReview, meta: { title: '每日复盘', icon: 'Notebook' } },
      { path: 'stocks/:code', name: 'stock-detail', component: loadStockDetail, meta: { title: '个股详情', icon: 'DataLine' } },
      { path: 'sentiment', name: 'sentiment', component: loadSentiment, meta: { title: '情绪面', icon: 'Sunny' } },
      { path: 'auction', name: 'auction', component: loadAuction, meta: { title: '竞价分析', icon: 'AlarmClock' } },
      { path: 'margin', name: 'margin', component: loadMargin, meta: { title: '融资融券', icon: 'Coin' } },
      { path: 'news', name: 'news', component: loadNews, meta: { title: '新闻面', icon: 'Document' } },
      { path: 'risk', name: 'risk', component: loadRisk, meta: { title: '风控中心', icon: 'Shield' } },
      { path: 'factors', name: 'factors', component: loadFactors, meta: { title: '因子引擎', icon: 'Cpu' } },
      { path: 'performance', name: 'performance', component: loadPerformance, meta: { title: '绩效中心', icon: 'Trophy' } },
      { path: 'paper', name: 'paper', component: loadPaper, meta: { title: '模拟盘', icon: 'Wallet' } },
      { path: 'backtest', name: 'backtest', component: loadBacktest, meta: { title: '回测引擎', icon: 'Timer' } },
      { path: 'model-lab', name: 'model-lab', component: loadModelLab, meta: { title: '模型实验室', icon: 'DataAnalysis' } },
      { path: 'governance', name: 'governance', component: loadGovernance, meta: { title: '数据治理', icon: 'Setting' } },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach((to, from, next) => {
  document.title = `${to.meta.title || '鹰爪'} — Claw 量化`
  next()
})

export default router
