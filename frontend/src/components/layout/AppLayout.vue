<template>
  <div class="app-shell" :class="{ 'sidebar-collapsed': sidebarCollapsed, 'is-mobile': isMobile }">
    <!-- 桌面端侧边栏 -->
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed, 'sidebar-hidden': isMobile }">
      <div class="sidebar-header">
        <div class="logo-wrap">
          <div class="logo-icon">
            <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
              <path d="M12 2L2 7L12 12L22 7L12 2Z" fill="currentColor" fill-opacity="0.2"/>
              <path d="M2 17L12 22L22 17" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
              <path d="M2 12L12 17L22 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            </svg>
          </div>
          <div class="logo-text-wrap">
            <span class="logo-text">Claw Quant</span>
            <span class="logo-sub">A股量化交易</span>
          </div>
        </div>
        <div class="logo-mini">
          <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M12 2L2 7L12 12L22 7L12 2Z" fill="currentColor" fill-opacity="0.2"/>
            <path d="M2 17L12 22L22 17" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
            <path d="M2 12L12 17L22 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
          </svg>
        </div>
      </div>

      <nav class="sidebar-nav">
        <template v-for="group in menuGroups" :key="group.label">
          <div class="nav-group-label">{{ group.label }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            :class="{ active: isActive(item.path) }"
            :title="item.label"
          >
            <span class="nav-icon-wrap">
              <el-icon :size="18"><component :is="item.icon" /></el-icon>
            </span>
            <span class="nav-label">{{ item.label }}</span>
            <span v-if="item.badge" class="nav-badge">{{ item.badge }}</span>
          </router-link>
        </template>
      </nav>

      <div class="sidebar-footer">
        <div class="connection-status">
          <span class="status-dot" :class="wsConnected ? 'online' : 'offline'"></span>
          <span class="status-text">{{ wsConnected ? '实时' : '离线' }}</span>
          <span v-if="tradeSessionLabel" class="status-chip session-chip">{{ tradeSessionLabel }}</span>
          <span class="status-chip trade-chip" :class="{ active: isTradeDay }">{{ isTradeDay ? '交易日' : '非交易日' }}</span>
        </div>
        <div class="sidebar-toggle" @click="sidebarCollapsed = !sidebarCollapsed">
          <el-icon :size="16">
            <component :is="sidebarCollapsed ? 'Expand' : 'Fold'" />
          </el-icon>
        </div>
      </div>
    </aside>

    <!-- 主内容区 -->
    <div class="main-area" :class="{ 'sidebar-collapsed': sidebarCollapsed, 'sidebar-hidden': isMobile }">
      <!-- 顶部导航栏 -->
      <header class="app-header">
        <div class="header-left">
          <el-icon class="menu-toggle mobile-only" :size="20" @click="mobileMenuVisible = !mobileMenuVisible">
            <Menu />
          </el-icon>
          <el-icon class="menu-toggle desktop-only" :size="18" @click="sidebarCollapsed = !sidebarCollapsed">
            <component :is="sidebarCollapsed ? 'Expand' : 'Fold'" />
          </el-icon>
        </div>
        <div class="header-right">
          <span v-if="tradeSessionLabel" class="session-badge">{{ tradeSessionLabel }}</span>
          <el-tag :type="isTradeDay ? 'success' : 'info'" size="small" effect="light" class="trade-day-tag">
            {{ isTradeDay ? '交易日' : '非交易日' }}
          </el-tag>
          <div class="header-actions">
            <el-tooltip content="刷新数据" placement="bottom">
              <el-button circle size="small" class="action-btn" @click="refreshData">
                <el-icon><Refresh /></el-icon>
              </el-button>
            </el-tooltip>
            <el-tooltip content="全屏" placement="bottom">
              <el-button circle size="small" class="action-btn" @click="toggleFullscreen">
                <el-icon><FullScreen /></el-icon>
              </el-button>
            </el-tooltip>
          </div>
        </div>
      </header>

      <!-- 移动端菜单遮罩 -->
      <transition name="fade">
        <div v-if="mobileMenuVisible && isMobile" class="mobile-menu-overlay" @click="mobileMenuVisible = false"></div>
      </transition>

      <!-- 移动端侧边菜单 -->
      <transition name="slide-left">
        <div v-if="mobileMenuVisible && isMobile" class="mobile-menu">
          <div class="mobile-menu-header">
            <div class="logo-wrap">
              <div class="logo-icon">
                <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
                  <path d="M12 2L2 7L12 12L22 7L12 2Z" fill="currentColor" fill-opacity="0.2"/>
                  <path d="M2 17L12 22L22 17" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                  <path d="M2 12L12 17L22 12" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                </svg>
              </div>
              <span class="logo-text">Claw Quant</span>
            </div>
            <el-icon class="close-btn" :size="20" @click="mobileMenuVisible = false">
              <Close />
            </el-icon>
          </div>
          <nav class="mobile-nav">
            <template v-for="group in menuGroups" :key="group.label">
              <div class="mobile-nav-group">{{ group.label }}</div>
              <router-link
                v-for="item in group.items"
                :key="item.path"
                :to="item.path"
                class="mobile-nav-item"
                :class="{ active: isActive(item.path) }"
                @click="mobileMenuVisible = false"
              >
                <el-icon :size="18"><component :is="item.icon" /></el-icon>
                <span>{{ item.label }}</span>
                <el-icon v-if="isActive(item.path)" class="active-indicator"><ArrowRight /></el-icon>
              </router-link>
            </template>
          </nav>
        </div>
      </transition>

      <!-- 主内容 -->
      <main class="app-main">
        <router-view v-slot="{ Component }">
          <transition name="fade-slide" mode="out-in">
            <component :is="Component" :key="route.fullPath" />
          </transition>
        </router-view>
      </main>

      <!-- 移动端底部导航 -->
      <nav class="mobile-tabbar" v-if="isMobile">
        <router-link 
          v-for="item in mobileTabs" 
          :key="item.path" 
          :to="item.path" 
          class="mobile-tab"
          :class="{ active: isActive(item.path) }"
        >
          <span class="tab-icon">
            <el-icon :size="20"><component :is="item.icon" /></el-icon>
          </span>
          <span class="tab-label">{{ item.label }}</span>
        </router-link>
      </nav>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useWebSocket } from '@/composables/useWebSocket'
import { getCalendarToday } from '@/api'
import { prefetchTenbaggerRoute } from '@/router'

const route = useRoute()
const { connected: wsConnected } = useWebSocket()

// 响应式状态
const sidebarCollapsed = ref(false)
const mobileMenuVisible = ref(false)
const isMobile = ref(false)

// 默认折叠侧边栏（桌面端）
const initSidebarState = () => {
  // 可以根据屏幕宽度设置默认状态
  const width = window.innerWidth
  if (width >= 1366 && width < 1920) {
    sidebarCollapsed.value = false
  } else if (width >= 1920) {
    sidebarCollapsed.value = false
  }
}
const isTradeDay = ref(false)
const tradeSession = ref('--')
let tenbaggerPrefetchHandle = null

// 检测移动端
const checkMobile = () => {
  isMobile.value = window.innerWidth <= 1023
  if (!isMobile.value) {
    mobileMenuVisible.value = false
  }
}

// 交易时段标签
const tradeSessionLabel = computed(() => {
  const map = {
    pre_market: '盘前',
    morning: '上午盘',
    midday_break: '午间',
    afternoon: '下午盘',
    after_hours: '盘后',
    night_session: '夜间',
    closed: '休市',
  }
  return map[tradeSession.value] || ''
})

// 路由激活状态
const isActive = (path) => {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

// 菜单分组
const menuGroups = [
  {
    label: '行情中心',
    items: [
      { path: '/', label: '行情总览', icon: 'TrendCharts' },
      { path: '/stocks', label: '个股中心', icon: 'Search' },
      { path: '/sectors', label: '板块营地', icon: 'PieChart' },
      { path: '/commodity-linkage', label: '商品联动', icon: 'Connection' },
      { path: '/tenbagger', label: '牛股雷达', icon: 'Aim' },
      { path: '/promotion', label: '晋级预测', icon: 'TopRight' },
      { path: '/daily-review', label: '每日复盘', icon: 'Notebook' },
    ],
  },
  {
    label: '分析工具',
    items: [
      { path: '/sentiment', label: '情绪面', icon: 'Sunny' },
      { path: '/auction', label: '竞价分析', icon: 'AlarmClock' },
      { path: '/margin', label: '融资融券', icon: 'Coin' },
      { path: '/news', label: '新闻面', icon: 'Document' },
    ],
  },
  {
    label: '交易系统',
    items: [
      { path: '/risk', label: '风控中心', icon: 'Lock' },
      { path: '/factors', label: '因子引擎', icon: 'Cpu' },
      { path: '/performance', label: '绩效中心', icon: 'Trophy' },
      { path: '/paper', label: '模拟盘', icon: 'Wallet' },
      { path: '/backtest', label: '回测引擎', icon: 'Timer' },
      { path: '/model-lab', label: '模型实验室', icon: 'DataAnalysis' },
      { path: '/governance', label: '数据治理', icon: 'Setting' },
    ],
  },
]

// 移动端底部标签
const mobileTabs = [
  { path: '/', label: '总览', icon: 'TrendCharts' },
  { path: '/sectors', label: '板块', icon: 'PieChart' },
  { path: '/sentiment', label: '情绪', icon: 'Sunny' },
  { path: '/risk', label: '风控', icon: 'Lock' },
  { path: '/paper', label: '模拟', icon: 'Wallet' },
]

// 刷新数据
const refreshData = () => {
  window.location.reload()
}

// 全屏切换
const toggleFullscreen = () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen()
  } else {
    document.exitFullscreen()
  }
}

// 监听窗口大小变化
onMounted(() => {
  checkMobile()
  initSidebarState()
  const startPrefetch = () => {
    tenbaggerPrefetchHandle = null
    if (route.path !== '/tenbagger') {
      prefetchTenbaggerRoute().catch(() => {})
    }
  }
  if (typeof window !== 'undefined' && typeof window.requestIdleCallback === 'function') {
    tenbaggerPrefetchHandle = window.requestIdleCallback(startPrefetch, { timeout: 1800 })
  } else {
    tenbaggerPrefetchHandle = window.setTimeout(startPrefetch, 400)
  }
  window.addEventListener('resize', checkMobile)
  
  // 获取交易日历
  getCalendarToday().then(data => {
    isTradeDay.value = data.is_trade_day
    tradeSession.value = data.session || '--'
  }).catch(() => {
    // ignore
  })
})

onUnmounted(() => {
  if (tenbaggerPrefetchHandle) {
    if (typeof window !== 'undefined' && typeof window.cancelIdleCallback === 'function') {
      window.cancelIdleCallback(tenbaggerPrefetchHandle)
    } else {
      window.clearTimeout(tenbaggerPrefetchHandle)
    }
    tenbaggerPrefetchHandle = null
  }
  window.removeEventListener('resize', checkMobile)
})

// 监听路由变化，关闭移动端菜单
watch(() => route.path, () => {
  mobileMenuVisible.value = false
})
</script>

<style lang="scss">
// ============================================
// 布局基础
// ============================================
.app-shell {
  display: flex;
  min-height: 100vh;
  background: var(--claw-bg);
}

// ============================================
// 侧边栏样式
// ============================================
.sidebar {
  width: var(--claw-sidebar-w);
  background: var(--claw-bg-card);
  border-right: 1px solid var(--claw-border);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
  position: fixed;
  left: 0;
  top: 0;
  bottom: 0;
  z-index: var(--z-fixed);

  &.collapsed {
    width: var(--claw-sidebar-collapsed-w);
  }

  &.sidebar-hidden {
    transform: translateX(-100%);
  }
}

.sidebar-header {
  height: var(--claw-header-h);
  display: flex;
  align-items: center;
  padding: 0 var(--spacing-4);
  border-bottom: 1px solid var(--claw-border-light);
}

.logo-wrap {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  overflow: hidden;
}

.logo-icon {
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--primary-500) 0%, var(--primary-600) 100%);
  color: white;
  flex-shrink: 0;
  box-shadow: 0 2px 8px rgba(14, 165, 233, 0.3);

  svg {
    width: 22px;
    height: 22px;
  }
}

.logo-mini {
  display: none;
  width: 40px;
  height: 40px;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  background: linear-gradient(135deg, var(--primary-500) 0%, var(--primary-600) 100%);
  color: white;
  margin: 0 auto;

  svg {
    width: 24px;
    height: 24px;
  }
}

.logo-text-wrap {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.logo-text {
  font-size: 1.125rem;
  font-weight: 700;
  color: var(--claw-text-primary);
  letter-spacing: -0.02em;
  white-space: nowrap;
}

.logo-sub {
  font-size: 0.6875rem;
  color: var(--claw-text-muted);
  white-space: nowrap;
}

// ============================================
// 导航菜单
// ============================================
.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  padding: var(--spacing-3);
  
  &::-webkit-scrollbar {
    width: 4px;
  }
  
  &::-webkit-scrollbar-thumb {
    background: transparent;
    border-radius: var(--radius-full);
  }
  
  &:hover::-webkit-scrollbar-thumb {
    background: var(--neutral-300);
  }
}

.nav-group-label {
  padding: var(--spacing-3) var(--spacing-3) var(--spacing-2);
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--claw-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  padding: var(--spacing-2) var(--spacing-3);
  margin-bottom: var(--spacing-1);
  color: var(--claw-text-secondary);
  text-decoration: none;
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
  position: relative;

  &:hover {
    background: var(--primary-50);
    color: var(--claw-primary);
  }

  &.active {
    background: linear-gradient(135deg, var(--primary-500) 0%, var(--primary-600) 100%);
    color: white;
    box-shadow: 0 2px 8px rgba(14, 165, 233, 0.3);

    .nav-icon-wrap {
      color: white;
    }
  }
}

// 侧边栏折叠时的样式 - 使用 !important 确保优先级
.sidebar.collapsed {
  .sidebar-header {
    padding: 0;
    justify-content: center;

    .logo-wrap {
      display: none !important;
    }

    .logo-mini {
      display: flex !important;
      margin: 0 auto;
    }
  }

  .nav-group-label {
    display: none !important;
  }

  .nav-item {
    justify-content: center;
    padding: var(--spacing-2);

    .nav-label {
      display: none !important;
    }

    .nav-badge {
      display: none !important;
    }
  }

  .sidebar-footer {
    padding: var(--spacing-2);

    .connection-status {
      display: none !important;
    }
  }
}

.nav-icon-wrap {
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: inherit;
  flex-shrink: 0;
}

.nav-label {
  font-size: 0.875rem;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.nav-badge {
  margin-left: auto;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  background: var(--error-500);
  color: white;
  font-size: 0.6875rem;
  font-weight: 600;
  border-radius: var(--radius-full);
  display: flex;
  align-items: center;
  justify-content: center;
}

// ============================================
// 侧边栏底部
// ============================================
.sidebar-footer {
  padding: var(--spacing-3);
  border-top: 1px solid var(--claw-border-light);
}

.connection-status {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  padding: var(--spacing-2) var(--spacing-3);
  margin-bottom: var(--spacing-2);
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: var(--radius-md);
  font-size: 0.75rem;
  color: var(--claw-text-muted);
}

.status-text {
  font-weight: 600;
  color: var(--neutral-500);
}

.status-chip {
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 0.6875rem;
  font-weight: 600;
  line-height: 1.1;
  color: var(--claw-text-secondary);
  background: #fff;
  border: 1px solid var(--claw-border);
}

.session-chip {
  color: var(--primary-700);
  background: var(--primary-50);
  border-color: var(--primary-200);
}

.trade-chip {
  color: var(--neutral-600);
  background: var(--neutral-100);
  border-color: var(--claw-border-light);
}

.trade-chip.active {
  color: var(--success-700);
  background: var(--success-50);
  border-color: var(--success-100);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;

  &.online {
    background: var(--success-500);
    box-shadow: 0 0 0 2px var(--success-100);
  }

  &.offline {
    background: var(--neutral-400);
    box-shadow: 0 0 0 2px var(--neutral-100);
  }
}

.sidebar-toggle {
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-md);
  cursor: pointer;
  color: var(--claw-text-muted);
  transition: all var(--transition-fast);

  &:hover {
    background: var(--neutral-100);
    color: var(--claw-primary);
  }
}

// ============================================
// 主内容区
// ============================================
.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  margin-left: var(--claw-sidebar-w);
}

// 侧边栏折叠状态
.main-area.sidebar-collapsed {
  margin-left: var(--claw-sidebar-collapsed-w);
}

// 侧边栏隐藏状态（移动端）
.main-area.sidebar-hidden {
  margin-left: 0;
}

// ============================================
// 顶部导航栏
// ============================================
.app-header {
  height: 54px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--spacing-5);
  background: var(--claw-bg-card);
  border-bottom: 1px solid var(--claw-border);
  position: sticky;
  top: 0;
  z-index: var(--z-sticky);

  @media (max-width: 1365px) {
    padding: 0 var(--spacing-4);
  }
}

@media (min-width: 1024px) {
  .app-header {
    display: none;
  }
}

.header-left,
.header-right {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
}

.menu-toggle {
  cursor: pointer;
  color: var(--claw-text-muted);
  padding: var(--spacing-2);
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);

  &:hover {
    background: var(--neutral-100);
    color: var(--claw-primary);
  }
}

.session-badge {
  padding: var(--spacing-1) var(--spacing-2);
  background: var(--primary-50);
  color: var(--primary-700);
  font-size: 0.6875rem;
  font-weight: 600;
  border-radius: var(--radius-sm);
  border: 1px solid var(--primary-200);
}

.trade-day-tag {
  font-weight: 500;
}

.header-actions {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
}

.action-btn {
  color: var(--claw-text-muted);

  &:hover {
    color: var(--claw-primary);
    border-color: var(--primary-300);
    background: var(--primary-50);
  }
}

// ============================================
// 移动端菜单
// ============================================
.mobile-menu-overlay {
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.5);
  backdrop-filter: blur(4px);
  z-index: var(--z-modal-backdrop);
}

.mobile-menu {
  position: fixed;
  left: 0;
  top: 0;
  bottom: 0;
  width: 280px;
  background: var(--claw-bg-card);
  z-index: var(--z-modal);
  display: flex;
  flex-direction: column;
  box-shadow: var(--shadow-xl);
}

.mobile-menu-header {
  height: var(--claw-header-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 var(--spacing-4);
  border-bottom: 1px solid var(--claw-border);

  .logo-wrap {
    .logo-icon {
      width: 32px;
      height: 32px;

      svg {
        width: 20px;
        height: 20px;
      }
    }

    .logo-text {
      font-size: 1rem;
    }
  }

  .close-btn {
    cursor: pointer;
    color: var(--claw-text-muted);
    padding: var(--spacing-2);
    border-radius: var(--radius-md);

    &:hover {
      background: var(--neutral-100);
      color: var(--claw-text-primary);
    }
  }
}

.mobile-nav {
  flex: 1;
  overflow-y: auto;
  padding: var(--spacing-3);
}

.mobile-nav-group {
  padding: var(--spacing-3) var(--spacing-3) var(--spacing-2);
  font-size: 0.6875rem;
  font-weight: 600;
  color: var(--claw-text-muted);
  text-transform: uppercase;
  letter-spacing: 0.05em;
}

.mobile-nav-item {
  display: flex;
  align-items: center;
  gap: var(--spacing-3);
  padding: var(--spacing-3);
  margin-bottom: var(--spacing-1);
  color: var(--claw-text-secondary);
  text-decoration: none;
  border-radius: var(--radius-md);
  transition: all var(--transition-fast);
  font-size: 0.9375rem;
  font-weight: 500;

  &:hover {
    background: var(--primary-50);
    color: var(--claw-primary);
  }

  &.active {
    background: var(--primary-50);
    color: var(--claw-primary);

    .active-indicator {
      margin-left: auto;
      color: var(--claw-primary);
    }
  }
}

// ============================================
// 主内容区域
// ============================================
.app-main {
  flex: 1;
  min-width: 0;
  overflow-x: hidden;
}

// ============================================
// 移动端底部导航
// ============================================
.mobile-tabbar {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  height: var(--claw-mobile-tab-h);
  background: var(--claw-bg-card);
  border-top: 1px solid var(--claw-border);
  display: flex;
  justify-content: space-around;
  align-items: center;
  padding-bottom: env(safe-area-inset-bottom);
  z-index: var(--z-fixed);
}

.mobile-tab {
  flex: 1;
  height: 100%;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 2px;
  color: var(--claw-text-muted);
  text-decoration: none;
  transition: all var(--transition-fast);
  position: relative;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 50%;
    transform: translateX(-50%);
    width: 0;
    height: 3px;
    background: var(--claw-primary);
    border-radius: 0 0 var(--radius-sm) var(--radius-sm);
    transition: width var(--transition-fast);
  }

  &.active {
    color: var(--claw-primary);

    &::before {
      width: 24px;
    }

    .tab-icon {
      transform: translateY(-2px);
    }
  }
}

.tab-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  transition: transform var(--transition-fast);
}

.tab-label {
  font-size: 0.6875rem;
  font-weight: 500;
}

// ============================================
// 过渡动画
// ============================================
.fade-enter-active,
.fade-leave-active {
  transition: opacity var(--transition-base);
}

.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}

.slide-left-enter-active,
.slide-left-leave-active {
  transition: transform var(--transition-slow);
}

.slide-left-enter-from,
.slide-left-leave-to {
  transform: translateX(-100%);
}

.fade-slide-enter-active,
.fade-slide-leave-active {
  transition: all var(--transition-base);
}

.fade-slide-enter-from {
  opacity: 0;
  transform: translateY(10px);
}

.fade-slide-leave-to {
  opacity: 0;
  transform: translateY(-10px);
}

// ============================================
// 响应式断点
// ============================================

// 超宽桌面 (≥1920px)
@media (min-width: 1920px) {
  .sidebar {
    width: 260px;

    &.collapsed {
      width: var(--claw-sidebar-collapsed-w);
    }
  }

  .main-area {
    margin-left: 260px;

    &.sidebar-collapsed {
      margin-left: var(--claw-sidebar-collapsed-w);
    }
  }
}

// 平板端 (768px - 1023px)
@media (max-width: 1023px) {
  .sidebar {
    transform: translateX(-100%);
  }

  .main-area {
    margin-left: 0;
  }

  .desktop-only {
    display: none !important;
  }
}

// 移动端 (< 768px)
@media (min-width: 768px) {
  .mobile-only {
    display: none !important;
  }
}

@media (max-width: 767px) {
  .app-header {
    height: 50px;
    padding: 0 var(--spacing-3);
  }

  .session-badge {
    display: none;
  }

  .trade-day-tag {
    display: none;
  }
}

// 小屏移动端 (< 375px)
@media (max-width: 374px) {
  .mobile-tab {
    .tab-label {
      font-size: 0.625rem;
    }
  }
}
</style>
