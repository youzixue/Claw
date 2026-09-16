<template>
  <div class="page-container">
    <div class="page-shell stock-center-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title">个股中心</h2>
          <div class="page-subtitle">按股票代码或名称查找 A 股，并进入现有个股详情页</div>
        </div>
      </div>

      <section class="search-panel">
        <div class="search-heading">
          <div class="search-mark">
            <el-icon :size="22"><Search /></el-icon>
          </div>
          <div>
            <h3>查找个股</h3>
            <p>支持 6 位股票代码、股票名称及带市场前后缀的代码</p>
          </div>
        </div>

        <div class="search-bar">
          <el-input
            ref="searchInputRef"
            v-model="keyword"
            size="large"
            clearable
            autocomplete="off"
            placeholder="例如：600519、贵州茅台、600519.SH"
            aria-label="搜索股票代码或名称"
            @keyup.enter="handleSearch"
            @clear="resetResults"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
          <el-button type="primary" size="large" :loading="loading" @click="handleSearch">
            搜索个股
          </el-button>
        </div>

        <div class="search-hint">
          输入精确代码或完整名称后按回车，可直接进入个股详情
        </div>
      </section>

      <section v-if="hasSearched" class="content-panel result-panel">
        <div class="section-header">
          <div>
            <h3>搜索结果</h3>
            <span v-if="results.length">找到 {{ results.length }} 只相关股票</span>
          </div>
          <span v-if="results.length" class="section-tip">点击任意结果查看详情</span>
        </div>

        <div v-loading="loading" class="result-body">
          <div v-if="results.length" class="stock-list">
            <button
              v-for="stock in results"
              :key="stock.code"
              type="button"
              class="stock-row"
              @click="openStock(stock)"
            >
              <div class="stock-identity">
                <strong>{{ stock.name || stock.code }}</strong>
                <span>{{ stock.code }}</span>
              </div>

              <div class="stock-tags">
                <el-tag size="small" effect="plain">{{ boardLabel(stock.board_type) }}</el-tag>
                <el-tag
                  v-if="stock.board_tag"
                  size="small"
                  effect="light"
                  :type="boardTagType(stock.board_tag)"
                >
                  {{ stockTagLabel(stock.board_tag) }}
                </el-tag>
                <el-tag v-if="stock.is_st" size="small" type="danger" effect="light">ST</el-tag>
                <el-tag v-if="stock.is_suspended" size="small" type="info" effect="light">停牌</el-tag>
                <el-tag v-if="stock.is_delisting" size="small" type="danger" effect="dark">退市风险</el-tag>
              </div>

              <div class="stock-quote">
                <strong>{{ formatPrice(stock.price) }}</strong>
                <span :class="changeColorClass(stock.change_pct)">
                  {{ formatChange(stock.change_pct) }}
                </span>
              </div>

              <el-icon class="row-arrow"><ArrowRight /></el-icon>
            </button>
          </div>

          <el-empty
            v-else-if="!loading"
            :image-size="72"
            :description="`未找到“${lastKeyword}”相关股票，请检查代码或名称`"
          >
            <el-button @click="focusSearch">重新输入</el-button>
          </el-empty>
        </div>
      </section>

      <section class="content-panel recent-panel">
        <div class="section-header">
          <div>
            <h3>最近访问</h3>
            <span>保存在当前浏览器中，方便再次进入</span>
          </div>
          <el-button
            v-if="recentStocks.length"
            text
            class="clear-recent-button"
            @click="clearRecent"
          >
            清空记录
          </el-button>
        </div>

        <div v-if="recentStocks.length" class="recent-grid">
          <button
            v-for="stock in recentStocks"
            :key="stock.code"
            type="button"
            class="recent-card"
            @click="openStock(stock)"
          >
            <div class="recent-main">
              <strong>{{ stock.name || stock.code }}</strong>
              <span>{{ stock.code }} · {{ boardLabel(stock.board_type) }}</span>
            </div>
            <el-icon><ArrowRight /></el-icon>
          </button>
        </div>

        <div v-else class="recent-empty">
          <el-icon :size="20"><Clock /></el-icon>
          <span>还没有访问记录，搜索并打开股票后会显示在这里</span>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup>
import { nextTick, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { searchStocks } from '@/api'
import { changeColorClass, formatChange, stockTagLabel } from '@/composables/useUtils'
import { notifyWarning } from '@/utils/message'

const RECENT_STORAGE_KEY = 'claw.stock-center.recent'
const RECENT_LIMIT = 8

const router = useRouter()
const searchInputRef = ref(null)
const keyword = ref('')
const lastKeyword = ref('')
const loading = ref(false)
const hasSearched = ref(false)
const results = ref([])
const recentStocks = ref([])

function normalizeSearchValue(value) {
  const compact = String(value || '').replace(/\s+/g, '')
  const prefixed = compact.match(/^(?:sh|sz|bj)(\d{6})$/i)
  if (prefixed) return prefixed[1]
  const suffixed = compact.match(/^(\d{6})\.(?:sh|sz|bj)$/i)
  if (suffixed) return suffixed[1]
  return compact
}

function isExactMatch(stock, value) {
  const normalized = normalizeSearchValue(value)
  return stock.code === normalized || stock.name === normalized
}

function boardLabel(boardType) {
  const labels = {
    main_sh: '沪市主板',
    main_sz: '深市主板',
    sme: '深市主板',
    gem: '创业板',
    star: '科创板',
    bse: '北交所',
  }
  return labels[boardType] || '其他市场'
}

function boardTagType(tag) {
  const types = {
    tradeable: 'success',
    observe_only: 'warning',
    blocked: 'danger',
    suspended: 'info',
  }
  return types[tag] || 'info'
}

function formatPrice(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return '--'
  return Number(value).toFixed(2)
}

function sanitizeStock(stock) {
  return {
    code: String(stock?.code || ''),
    name: String(stock?.name || ''),
    board_type: String(stock?.board_type || ''),
    board_tag: String(stock?.board_tag || ''),
  }
}

function persistRecent() {
  try {
    window.localStorage.setItem(RECENT_STORAGE_KEY, JSON.stringify(recentStocks.value))
  } catch {
    // 浏览器禁用本地存储时不影响搜索与跳转
  }
}

function loadRecent() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(RECENT_STORAGE_KEY) || '[]')
    if (!Array.isArray(saved)) return
    recentStocks.value = saved
      .map(sanitizeStock)
      .filter(stock => /^\d{6}$/.test(stock.code))
      .slice(0, RECENT_LIMIT)
  } catch {
    recentStocks.value = []
  }
}

function rememberStock(stock) {
  const item = sanitizeStock(stock)
  if (!/^\d{6}$/.test(item.code)) return
  recentStocks.value = [
    item,
    ...recentStocks.value.filter(existing => existing.code !== item.code),
  ].slice(0, RECENT_LIMIT)
  persistRecent()
}

function openStock(stock) {
  if (!stock?.code) return
  rememberStock(stock)
  router.push({ name: 'stock-detail', params: { code: stock.code } })
}

async function handleSearch() {
  const query = keyword.value.trim()
  if (!query) {
    notifyWarning('请输入股票代码或名称')
    focusSearch()
    return
  }

  loading.value = true
  hasSearched.value = true
  lastKeyword.value = query

  try {
    const response = await searchStocks({ keyword: query, limit: 20 })
    results.value = Array.isArray(response?.stocks) ? response.stocks : []
    const exact = results.value.find(stock => isExactMatch(stock, query))
    if (exact) {
      openStock(exact)
    }
  } catch {
    results.value = []
  } finally {
    loading.value = false
  }
}

function resetResults() {
  if (keyword.value) return
  hasSearched.value = false
  lastKeyword.value = ''
  results.value = []
}

function focusSearch() {
  nextTick(() => searchInputRef.value?.focus())
}

function clearRecent() {
  recentStocks.value = []
  persistRecent()
}

onMounted(() => {
  loadRecent()
  focusSearch()
})
</script>

<style scoped lang="scss">
.stock-center-page {
  max-width: 1180px;
  margin: 0 auto;
}

.search-panel,
.content-panel {
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--claw-shadow-sm);
}

.search-panel {
  padding: 28px;
}

.search-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 22px;

  h3,
  p {
    margin: 0;
  }

  h3 {
    color: var(--claw-text-primary);
    font-size: 18px;
    font-weight: 700;
  }

  p {
    margin-top: 4px;
    color: var(--claw-text-muted);
    font-size: 13px;
  }
}

.search-mark {
  width: 44px;
  height: 44px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: var(--claw-primary);
  background: var(--primary-50);
  border: 1px solid var(--primary-100);
  border-radius: 12px;
}

.search-bar {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 116px;
  gap: 12px;
  max-width: 820px;

  :deep(.el-input__wrapper) {
    min-height: 46px;
    border-radius: 10px;
  }

  .el-button {
    min-height: 46px;
    border-radius: 10px;
    font-weight: 600;
  }
}

.search-hint {
  margin-top: 10px;
  color: var(--claw-text-muted);
  font-size: 12px;
}

.content-panel {
  padding: 20px;
}

.section-header {
  min-height: 40px;
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 14px;

  h3 {
    margin: 0;
    color: var(--claw-text-primary);
    font-size: 16px;
    font-weight: 700;
  }

  span {
    display: block;
    margin-top: 4px;
    color: var(--claw-text-muted);
    font-size: 12px;
  }

  .section-tip {
    margin-top: 2px;
  }
}

.result-body {
  min-height: 96px;
}

.stock-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.stock-row {
  width: 100%;
  min-height: 66px;
  display: grid;
  grid-template-columns: minmax(160px, 0.9fr) minmax(250px, 1.4fr) 130px 24px;
  align-items: center;
  gap: 16px;
  padding: 12px 14px;
  font: inherit;
  color: inherit;
  text-align: left;
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast), transform var(--transition-fast);

  &:hover {
    background: var(--primary-50);
    border-color: var(--primary-200);
    transform: translateY(-1px);

    .row-arrow {
      color: var(--claw-primary);
      transform: translateX(2px);
    }
  }
}

.stock-identity {
  display: flex;
  flex-direction: column;
  gap: 4px;

  strong {
    color: var(--claw-text-primary);
    font-size: 15px;
  }

  span {
    color: var(--claw-text-muted);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
  }
}

.stock-tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.stock-quote {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 3px;
  font-variant-numeric: tabular-nums;

  strong {
    color: var(--claw-text-primary);
    font-size: 16px;
  }

  span {
    font-size: 13px;
    font-weight: 600;
  }
}

.row-arrow {
  color: var(--claw-text-muted);
  transition: color var(--transition-fast), transform var(--transition-fast);
}

.recent-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
}

.recent-card {
  min-height: 72px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 14px;
  font: inherit;
  color: var(--claw-text-muted);
  text-align: left;
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color var(--transition-fast), background var(--transition-fast);

  &:hover {
    color: var(--claw-primary);
    background: var(--primary-50);
    border-color: var(--primary-200);
  }
}

.recent-main {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 5px;

  strong {
    overflow: hidden;
    color: var(--claw-text-primary);
    font-size: 14px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  span {
    overflow: hidden;
    color: var(--claw-text-muted);
    font-size: 11px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.recent-empty {
  min-height: 80px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--claw-text-muted);
  background: var(--neutral-50);
  border: 1px dashed var(--claw-border);
  border-radius: 10px;
  font-size: 13px;
}

@media (max-width: 1023px) {
  .recent-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .stock-row {
    grid-template-columns: minmax(150px, 0.8fr) minmax(200px, 1.2fr) 110px 20px;
    gap: 10px;
  }
}

@media (max-width: 767px) {
  .search-panel {
    padding: 18px;
  }

  .search-heading {
    align-items: flex-start;
  }

  .search-bar {
    grid-template-columns: 1fr;
  }

  .content-panel {
    padding: 16px;
  }

  .section-tip {
    display: none !important;
  }

  .stock-row {
    grid-template-columns: minmax(0, 1fr) auto 18px;
  }

  .stock-tags {
    grid-column: 1 / -1;
    grid-row: 2;
  }

  .stock-quote {
    grid-column: 2;
    grid-row: 1;
  }

  .row-arrow {
    grid-column: 3;
    grid-row: 1;
  }

  .recent-grid {
    grid-template-columns: 1fr;
  }
}
</style>
