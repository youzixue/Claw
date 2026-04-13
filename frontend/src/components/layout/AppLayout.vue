<template>
  <div class="app-shell">
    <aside class="sidebar" :class="{ collapsed: sidebarCollapsed }">
      <div class="sidebar-header">
        <div class="logo-wrap" v-show="!sidebarCollapsed">
          <span class="logo-icon">◧</span>
          <div class="logo-text-wrap">
            <span class="logo-text">Claw Quant</span>
            <span class="logo-sub">A股量化交易系统</span>
          </div>
        </div>
        <span class="logo-mini" v-show="sidebarCollapsed">◧</span>
      </div>

      <nav class="sidebar-nav">
        <template v-for="group in menuGroups" :key="group.label">
          <div class="nav-group-label" v-show="!sidebarCollapsed">{{ group.label }}</div>
          <router-link
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="nav-item"
            :class="{ active: isActive(item.path) }"
          >
            <el-icon :size="18"><component :is="item.icon" /></el-icon>
            <span class="nav-label" v-show="!sidebarCollapsed">{{ item.label }}</span>
          </router-link>
        </template>
      </nav>

      <div class="sidebar-toggle" @click="sidebarCollapsed = !sidebarCollapsed">
        <el-icon :size="15">
          <component :is="sidebarCollapsed ? 'Expand' : 'Fold'" />
        </el-icon>
      </div>
    </aside>

    <div class="main-area">
      <header class="app-header">
        <div class="header-left">
          <el-icon class="menu-toggle only-mobile" :size="18" @click="mobileMenuVisible = !mobileMenuVisible">
            <Expand />
          </el-icon>
          <el-icon class="menu-toggle only-desktop" :size="18" @click="sidebarCollapsed = !sidebarCollapsed">
            <component :is="sidebarCollapsed ? 'Expand' : 'Fold'" />
          </el-icon>
          <span class="page-name">{{ currentTitle }}</span>
        </div>
        <div class="header-right">
          <el-tag :type="wsConnected ? 'primary' : 'info'" size="small" round effect="plain">
            <span class="status-dot" :class="wsConnected ? 'dot-live' : 'dot-offline'"></span>
            {{ wsConnected ? '实时连接' : '离线' }}
          </el-tag>
          <el-tag :type="isTradeDay ? 'primary' : 'info'" size="small" round effect="plain">
            {{ isTradeDay ? '交易日' : '非交易日' }}
          </el-tag>
          <el-tag v-if="tradeSessionLabel" type="warning" size="small" round effect="plain">
            {{ tradeSessionLabel }}
          </el-tag>
        </div>
      </header>

      <div v-if="mobileMenuVisible" class="mobile-menu-sheet">
        <div class="mobile-menu-title">模块导航</div>
        <div class="mobile-menu-list">
          <router-link
            v-for="item in flatMenuItems"
            :key="item.path"
            :to="item.path"
            class="mobile-menu-item"
            :class="{ active: isActive(item.path) }"
            @click="mobileMenuVisible = false"
          >
            <el-icon :size="18"><component :is="item.icon" /></el-icon>
            <span>{{ item.label }}</span>
          </router-link>
        </div>
      </div>

      <main class="app-main">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>

      <nav class="mobile-tabbar">
        <router-link v-for="item in mobileTabs" :key="item.path" :to="item.path" class="mobile-tab" :class="{ active: isActive(item.path) }">
          <el-icon :size="18"><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </router-link>
      </nav>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { useWebSocket } from '@/composables/useWebSocket'
import { getCalendarToday } from '@/api'

const route = useRoute()
const { connected: wsConnected } = useWebSocket()

const sidebarCollapsed = ref(false)
const mobileMenuVisible = ref(false)
const isTradeDay = ref(false)
const tradeSession = ref('--')

const currentTitle = computed(() => route.meta.title || 'Claw Quant')

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

const isActive = (path) => {
  if (path === '/') return route.path === '/'
  return route.path.startsWith(path)
}

const menuGroups = [
  {
    label: '行情',
    items: [
      { path: '/', label: '总览', icon: 'TrendCharts' },
      { path: '/sectors', label: '板块', icon: 'PieChart' },
      { path: '/tenbagger', label: '雷达', icon: 'Aim' },
      { path: '/promotion', label: '晋级', icon: 'TopRight' },
    ],
  },
  {
    label: '分析',
    items: [
      { path: '/sentiment', label: '情绪', icon: 'Sunny' },
      { path: '/auction', label: '竞价', icon: 'AlarmClock' },
      { path: '/margin', label: '两融', icon: 'Coin' },
      { path: '/news', label: '新闻', icon: 'Document' },
    ],
  },
  {
    label: '系统',
    items: [
      { path: '/risk', label: '风控', icon: 'Lock' },
      { path: '/factors', label: '因子', icon: 'Cpu' },
      { path: '/performance', label: '绩效', icon: 'Trophy' },
      { path: '/paper', label: '模拟盘', icon: 'Wallet' },
      { path: '/backtest', label: '回测', icon: 'Timer' },
      { path: '/governance', label: '治理', icon: 'Setting' },
    ],
  },
]

const flatMenuItems = computed(() => menuGroups.flatMap(group => group.items))
const mobileTabs = computed(() => [
  { path: '/', label: '总览', icon: 'TrendCharts' },
  { path: '/sectors', label: '板块', icon: 'PieChart' },
  { path: '/sentiment', label: '情绪', icon: 'Sunny' },
  { path: '/risk', label: '风控', icon: 'Lock' },
  { path: '/paper', label: '模拟', icon: 'Wallet' },
])

onMounted(async () => {
  try {
    const data = await getCalendarToday()
    isTradeDay.value = data.is_trade_day
    tradeSession.value = data.session || '--'
  } catch {
    // ignore
  }
})
</script>

<style scoped lang="scss">
.app-shell {
  display: flex;
  min-height: 100vh;
  background: var(--claw-bg);
}

.sidebar {
  width: var(--claw-sidebar-w);
  background: var(--claw-bg-sidebar);
  border-right: 1px solid var(--claw-border);
  display: flex;
  flex-direction: column;
  transition: width 0.2s ease;
  flex-shrink: 0;
  position: relative;

  &.collapsed {
    width: 76px;
  }
}

.sidebar-header {
  height: var(--claw-header-h);
  display: flex;
  align-items: center;
  justify-content: center;
  border-bottom: 1px solid var(--claw-border-light);
  padding: 0 14px;
}

.logo-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
}

.logo-icon,
.logo-mini {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  background: linear-gradient(180deg, #eef4ff 0%, #e1e9ff 100%);
  color: var(--claw-primary);
  font-size: 16px;
  font-weight: 700;
  border: 1px solid #d9e5ff;
}

.logo-text-wrap {
  display: flex;
  flex-direction: column;
}

.logo-text {
  font-size: 15px;
  font-weight: 700;
  color: var(--claw-text);
}

.logo-sub {
  font-size: 11px;
  color: var(--claw-text-muted);
}

.sidebar-nav {
  flex: 1;
  overflow-y: auto;
  padding: 12px 10px 16px;
}

.nav-group-label {
  font-size: 11px;
  font-weight: 600;
  color: var(--claw-text-muted);
  padding: 12px 12px 8px;
}

.nav-item {
  min-height: 44px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  margin: 3px 0;
  color: var(--claw-text-secondary);
  text-decoration: none;
  border-radius: 10px;
  transition: all 0.16s ease;

  &:hover {
    background: linear-gradient(180deg, #f6f9ff 0%, #eef4ff 100%);
    color: var(--claw-primary);
    transform: translateY(-1px);
  }

  &.active {
    color: var(--claw-primary);
    background: #e1e9ff;
    box-shadow: inset 0 0 0 1px #cfe0ff;
    font-weight: 600;
  }
}

.sidebar-toggle {
  min-height: 48px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-top: 1px solid var(--claw-border-light);
  cursor: pointer;
  color: var(--claw-text-muted);
}

.main-area {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.app-header {
  height: var(--claw-header-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
  background: var(--claw-bg-header);
  border-bottom: 1px solid var(--claw-border-light);
  backdrop-filter: blur(14px);
  position: sticky;
  top: 0;
  z-index: 20;
}

.header-left,
.header-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.page-name {
  font-size: 16px;
  font-weight: 600;
}

.menu-toggle {
  cursor: pointer;
  color: var(--claw-text-muted);
}

.status-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
  margin-right: 4px;
}

.dot-live { background: var(--claw-primary); }
.dot-offline { background: var(--claw-text-muted); }

.app-main {
  flex: 1;
  min-width: 0;
}

.mobile-menu-sheet,
.mobile-tabbar,
.only-mobile {
  display: none;
}

.fade-enter-active, .fade-leave-active { transition: opacity 0.15s ease; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

@media (max-width: 1024px) {
  .sidebar { width: 220px; }
}

@media (max-width: 768px) {
  .sidebar,
  .only-desktop {
    display: none;
  }

  .only-mobile,
  .mobile-tabbar {
    display: flex;
  }

  .app-header {
    padding: 0 14px;
  }

  .header-right {
    gap: 6px;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .mobile-menu-sheet {
    display: block;
    margin: 12px 14px 0;
    padding: 14px;
    background: rgba(255,255,255,0.96);
    border: 1px solid var(--claw-border);
    border-radius: 12px;
    box-shadow: var(--claw-shadow-md);
  }

  .mobile-menu-title {
    font-size: 12px;
    color: var(--claw-text-muted);
    margin-bottom: 10px;
  }

  .mobile-menu-list {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }

  .mobile-menu-item {
    min-height: 44px;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-radius: 10px;
    text-decoration: none;
    color: var(--claw-text-secondary);
    background: #f8faff;
    border: 1px solid var(--claw-border-light);
  }

  .mobile-menu-item.active {
    background: #e1e9ff;
    color: var(--claw-primary);
    border-color: #cfe0ff;
  }

  .mobile-tabbar {
    position: sticky;
    bottom: 0;
    z-index: 30;
    height: var(--claw-mobile-tab-h);
    background: rgba(255,255,255,0.96);
    backdrop-filter: blur(18px);
    border-top: 1px solid var(--claw-border-light);
    justify-content: space-around;
    align-items: center;
    padding-bottom: env(safe-area-inset-bottom);
  }

  .mobile-tab {
    min-width: 56px;
    min-height: 44px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 4px;
    color: var(--claw-text-muted);
    text-decoration: none;
    font-size: 11px;
  }

  .mobile-tab.active {
    color: var(--claw-primary);
  }
}
</style>
