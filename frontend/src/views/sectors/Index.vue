<template>
  <div class="page-container">
    <div class="page-shell sectors-page">
      <!-- 页面头部 -->
      <div class="page-hero">
        <div>
          <h2 class="page-title">
            <el-icon class="title-icon"><DataBoard /></el-icon>
            板块营地
          </h2>
          <div class="page-subtitle">围绕强弱、轮动、生命周期与主线状态做统一观察</div>
        </div>
        <div class="hero-chip">
          <el-icon><Grid /></el-icon>
          <span>{{ categoryLabel }}板块视图</span>
        </div>
      </div>

      <!-- 板块类型切换 -->
      <div class="category-tabs">
        <el-radio-group v-model="sectorCategory" size="default" @change="onCategoryChange">
          <el-radio-button value="concept">
            <el-icon><Collection /></el-icon>
            <span>概念板块</span>
          </el-radio-button>
          <el-radio-button value="industry">
            <el-icon><OfficeBuilding /></el-icon>
            <span>行业板块</span>
          </el-radio-button>
        </el-radio-group>
        <span class="count-badge">共 {{ sectorCount }} 个{{ categoryLabel }}</span>
      </div>

      <!-- 概览统计 -->
      <div class="sectors-metrics-panel" v-if="stats.total > 0">
        <div class="stats-grid">
          <div class="stat-card-v2">
            <div class="stat-icon-wrap">
              <el-icon :size="20"><Grid /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-label">{{ categoryLabel }}总数</div>
              <div class="stat-value">{{ sectorCount }}</div>
            </div>
          </div>
          <div class="stat-card-v2 hot">
            <div class="stat-icon-wrap hot-icon">
              <el-icon :size="20"><TrendCharts /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-label">热门板块</div>
              <div class="stat-value text-up">{{ stats.hot }}</div>
              <div class="stat-desc">资金流入&gt;10亿</div>
            </div>
          </div>
          <div class="stat-card-v2" :class="stats.totalFundFlow >= 0 ? 'inflow' : 'outflow'">
            <div class="stat-icon-wrap flow-icon">
              <el-icon :size="20"><Money /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-label">资金净流入</div>
              <div class="stat-value" :class="stats.totalFundFlow >= 0 ? 'text-up' : 'text-down'">
                {{ formatFundFlow(stats.totalFundFlow) }}
              </div>
            </div>
          </div>
          <div class="stat-card-v2">
            <div class="stat-icon-wrap">
              <el-icon :size="20"><Calendar /></el-icon>
            </div>
            <div class="stat-content">
              <div class="stat-label">数据日期</div>
              <div class="stat-value date">{{ stats.tradeDate }}</div>
            </div>
          </div>
        </div>
      </div>

      <!-- 子Tab: 强弱/轮动/持续性/生命周期 -->
      <el-tabs v-model="activeSubTab" class="subtabs-shell">
        <!-- 板块生命周期 -->
        <el-tab-pane label="生命周期" name="lifecycle">
          <div v-if="loading || lifecycleLoading" class="loading-container">
            <el-skeleton :rows="8" animated />
          </div>
          <template v-else>
            <!-- 状态筛选 -->
            <div class="state-filters mb-16">
              <el-radio-group v-model="lifecycleStateFilter" size="small" @change="onLifecycleFilterChange">
                <el-radio-button value="">全部</el-radio-button>
                <el-radio-button value="emerging">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-emerging"><Flag /></el-icon>
                    刚启动
                  </span>
                </el-radio-button>
                <el-radio-button value="accelerating">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-accelerating"><TopRight /></el-icon>
                    加速
                  </span>
                </el-radio-button>
                <el-radio-button value="climax">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-climax"><TrendCharts /></el-icon>
                    高潮
                  </span>
                </el-radio-button>
                <el-radio-button value="diverging">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-diverging"><Sort /></el-icon>
                    分化
                  </span>
                </el-radio-button>
                <el-radio-button value="declining">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-declining"><BottomRight /></el-icon>
                    退潮
                  </span>
                </el-radio-button>
                <el-radio-button value="one_day">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-one-day"><Timer /></el-icon>
                    一日游
                  </span>
                </el-radio-button>
                <el-radio-button value="capital_probe">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-capital-probe"><TrendCharts /></el-icon>
                    资金试探
                  </span>
                </el-radio-button>
                <el-radio-button value="dormant">
                  <span class="state-pill-label">
                    <el-icon class="state-pill-icon icon-dormant"><MoonNight /></el-icon>
                    休眠
                  </span>
                </el-radio-button>
              </el-radio-group>
            </div>

            <!-- 状态汇总 -->
            <div class="lc-summary-bar mb-16" v-if="lifecycleStats">
              <el-tooltip
                v-for="(stat, state) in lifecycleStats"
                :key="state"
                placement="top"
                :show-after="120"
              >
                <template #content>
                  <div style="max-width:240px;line-height:1.6;font-size:13px">{{ stateDesc(state) }}</div>
                </template>
                <span 
                  class="lc-stat-item"
                  @click="lifecycleStateFilter = state; onLifecycleFilterChange()"
                  :class="{ active: lifecycleStateFilter === state }"
                >
                  <el-icon class="lc-state-icon" :class="'lc-state-icon-' + state">
                    <component :is="stateIcon(state)" />
                  </el-icon>
                  <span class="lc-name">{{ stateLabel(state) }}</span>
                  <b>{{ stat.count }}</b>
                  <i class="lc-info" title="查看状态说明">?</i>
                </span>
              </el-tooltip>
            </div>

            <!-- 生命周期表格 -->
            <div class="table-container">
              <el-table :data="lifecycleList" stripe size="small" empty-text="暂无生命周期数据" class="lifecycle-table">
                <el-table-column prop="sector_name" label="板块" min-width="170">
                  <template #default="{ row }">
                    <div class="sector-name-cell">
                      <span class="sector-name">{{ row.sector_name }}</span>
                      <el-tag
                        v-if="row.main_line_status && row.main_line_status !== 'none'"
                        :type="mainLineTagType(row.main_line_status)"
                        size="small"
                        effect="light"
                        class="main-line-tag"
                      >
                        {{ row.main_line_label }}
                      </el-tag>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column prop="state_label" label="状态" width="120" align="center">
                  <template #default="{ row }">
                    <el-tooltip placement="top" :show-after="120">
                      <template #content>
                        <div class="state-reason-tooltip">
                          <div class="state-reason-title">{{ displayStateLabel(row) }}判定依据</div>
                          <div class="state-reason-line">涨停/首板/连板: {{ displayLimitUpCount(row) }} / {{ displayFirstBoardCount(row) }} / {{ displayConsecutiveBoardCount(row) }}</div>
                          <div class="state-reason-line">最高板: {{ displayMaxBoardHeight(row) > 0 ? (displayMaxBoardHeight(row) >= 2 ? displayMaxBoardHeight(row) + '连板' : '首板') : '-' }}</div>
                          <div class="state-reason-line">板块资金净额: {{ formatFundFlow(row.fund_flow) }}</div>
                          <div class="state-reason-line">强度分: {{ row.strength_score != null ? Math.round(row.strength_score) : (row.state_score ?? '-') }}</div>
                          <div class="state-reason-line">K线信号: {{ klineJudgeHint(row) }}</div>
                          <div v-if="isCapitalProbe(row)" class="state-reason-line text-warning">辅助信号: 资金试探，说明资金与趋势先启动，但涨停结构尚未成型。</div>
                          <div v-if="row.attribution_confidence_label && row.attribution_confidence !== 'none'" class="state-reason-line">归因置信度: {{ row.attribution_confidence_label }}</div>
                          <div v-if="row.attribution_confidence && row.attribution_confidence !== 'none'" class="state-reason-line text-muted">
                            {{ attributionConfidenceHint(row.attribution_confidence) }}
                          </div>
                          <div v-if="row.attributed_reason_samples?.length" class="state-reason-line">归因原因: {{ row.attributed_reason_samples.join('、') }}</div>
                          <div v-if="hasLifecycleRawFallback(row)" class="state-reason-line text-warning">映射参考(不参与状态判断): {{ row.raw_limit_up_count || 0 }} / {{ row.raw_first_board_count || 0 }} / {{ row.raw_consecutive_board_count || 0 }}，最高板 {{ row.raw_max_board_height > 0 ? (row.raw_max_board_height >= 2 ? row.raw_max_board_height + '连板' : '首板') : '-' }}</div>
                          <div v-if="hasLifecycleRawFallback(row) && row.raw_reason_samples?.length" class="state-reason-line text-warning">原始参考原因: {{ row.raw_reason_samples.join('、') }}</div>
                          <div class="state-reason-line">主生命周期: {{ stateLabel(row.lifecycle_state) }}</div>
                          <div class="state-reason-line">{{ stateJudgeHint(row) }}</div>
                        </div>
                      </template>
                      <el-tag :type="displayStateTagType(row)" size="small" effect="light" round class="lifecycle-state-tag">
                        <span class="state-tag-content">
                          <el-icon class="state-tag-icon" :class="'state-tag-icon-' + displayStateKey(row)">
                            <component :is="stateIcon(displayStateKey(row))" />
                          </el-icon>
                          {{ displayStateLabel(row) }}
                        </span>
                      </el-tag>
                    </el-tooltip>
                  </template>
                </el-table-column>
                <el-table-column prop="change_pct" label="涨跌" width="110" align="right">
                  <template #default="{ row }">
                    <span v-if="row.change_pct != null && row.change_pct != 0" :class="changeColorClass(row.change_pct)">
                      {{ formatChange(row.change_pct) }}
                    </span>
                    <span v-else class="text-muted">-</span>
                  </template>
                </el-table-column>
                <el-table-column prop="state_score" label="强度" width="80" align="center">
                  <template #default="{ row }">
                    <span :class="strengthClass(row.strength_score ?? row.state_score)">
                      {{ row.strength_score != null ? Math.round(row.strength_score) : (row.state_score ?? '-') }}
                    </span>
                  </template>
                </el-table-column>
                <el-table-column label="涨停/首板/连板" width="170" align="center">
                  <template #default="{ row }">
                    <el-popover placement="right" :width="400" trigger="hover" v-if="displayLadderStocks(row)?.length">
                      <template #reference>
                        <span class="cursor-pointer limit-up-stats">
                          <span class="text-up">{{ displayLimitUpCount(row) ?? '-' }}</span>
                          <span class="separator">/</span>
                          <span>{{ displayFirstBoardCount(row) ?? '-' }}</span>
                          <span class="separator">/</span>
                          <span class="text-warning">{{ displayConsecutiveBoardCount(row) ?? '-' }}</span>
                        </span>
                      </template>
                      <div class="ladder-popover">
                        <div class="ladder-header">
                          连板梯队 (涨停{{ displayLimitUpCount(row) }}/首板{{ displayFirstBoardCount(row) }}/连板{{ displayConsecutiveBoardCount(row) }})
                        </div>
                        <div v-for="ladder in displayLadderStocks(row)" :key="ladder.height" class="ladder-row">
                          <span class="ladder-height" :class="heightClass(ladder.height)">
                            {{ ladder.height >= 2 ? ladder.height + '连板' : '首板' }}
                          </span>
                          <span class="ladder-stocks">
                            <el-tag 
                              v-for="s in ladder.stocks.slice(0, 8)" 
                              :key="s.code" 
                              size="small" 
                              :type="ladder.height >= 3 ? 'danger' : 'warning'" 
                              class="stock-tag cursor-pointer" 
                              @click="goStock(s.code)"
                            >
                              {{ s.name }}
                            </el-tag>
                            <span v-if="ladder.stocks.length > 8" class="text-muted">+{{ ladder.stocks.length - 8 }}</span>
                          </span>
                        </div>
                        <div v-if="hasLifecycleRawFallback(row)" class="ladder-note">
                          映射参考(不参与状态判断): {{ row.raw_limit_up_count || 0 }}/{{ row.raw_first_board_count || 0 }}/{{ row.raw_consecutive_board_count || 0 }}
                          <span v-if="row.raw_reason_samples?.length">；原因: {{ row.raw_reason_samples.join('、') }}</span>
                        </div>
                      </div>
                    </el-popover>
                    <template v-else>
                      <span class="limit-up-stats">
                        <span class="text-up">{{ displayLimitUpCount(row) ?? '-' }}</span>
                        <span class="separator">/</span>
                        <span>{{ displayFirstBoardCount(row) ?? '-' }}</span>
                        <span class="separator">/</span>
                        <span class="text-warning">{{ displayConsecutiveBoardCount(row) ?? '-' }}</span>
                      </span>
                    </template>
                  </template>
                </el-table-column>
                <el-table-column prop="max_board_height" label="最高板" width="110" align="center">
                  <template #default="{ row }">
                    <template v-if="displayMaxBoardHeight(row) > 0 && displayLeaderStocks(row)?.length">
                      <span :class="heightClass(displayMaxBoardHeight(row))" class="cursor-pointer board-height" @click="goStock(displayLeaderStocks(row)[0].code)">
                        {{ displayMaxBoardHeight(row) >= 2 ? displayMaxBoardHeight(row) + '连板' : '首板' }}
                      </span>
                    </template>
                    <span v-else-if="displayMaxBoardHeight(row) > 0" :class="heightClass(displayMaxBoardHeight(row))" class="board-height">
                      {{ displayMaxBoardHeight(row) >= 2 ? displayMaxBoardHeight(row) + '连板' : '首板' }}
                    </span>
                    <span v-else class="text-muted">-</span>
                  </template>
                </el-table-column>
                <el-table-column label="龙头股" width="220" align="center">
                  <template #default="{ row }">
                    <template v-if="displayLeaderStocks(row) && displayLeaderStocks(row).length > 0">
                      <el-tag size="default" type="danger" effect="light" class="leader-tag cursor-pointer" @click="goStock(displayLeaderStocks(row)[0].code)">
                        {{ displayLeaderStocks(row)[0].name }}
                        <span class="leader-height">({{ displayLeaderStocks(row)[0].height >= 2 ? displayLeaderStocks(row)[0].height + '连板' : '首板' }})</span>
                      </el-tag>
                    </template>
                    <span v-else class="text-muted">-</span>
                  </template>
                </el-table-column>
                <el-table-column prop="fund_flow" label="板块资金净额(亿)" width="150" align="right">
                  <template #default="{ row }">
                    <span v-if="row.fund_flow != null && row.fund_flow !== ''" :class="changeColorClass(row.fund_flow)">
                      {{ formatFundFlow(row.fund_flow) }}
                    </span>
                    <span v-else class="text-muted">-</span>
                  </template>
                </el-table-column>
                <el-table-column prop="quality_score" label="质量分" width="90" align="center">
                  <template #default="{ row }">
                    <span v-if="row.quality_score != null && row.quality_score > 0" :class="qualityClass(row.quality_score)">
                      {{ Math.round(row.quality_score) }}
                    </span>
                    <span v-else class="text-muted">-</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <div class="pagination-wrap" v-if="lifecycleTotal > pageSize">
              <div class="pagination-total">共 {{ lifecycleTotal }} 个{{ categoryLabel }}</div>
              <el-pagination
                v-model:current-page="lifecyclePage"
                :page-size="pageSize"
                :total="lifecycleTotal"
                layout="prev, pager, next"
                size="small"
                @current-change="onLifecyclePageChange"
              />
            </div>
          </template>
        </el-tab-pane>

        <!-- 板块轮动日历 -->
        <el-tab-pane label="轮动日历" name="calendar">
          <div v-if="loading" class="loading-container">
            <el-skeleton :rows="10" animated />
          </div>
          <template v-else>
            <div class="calendar-toolbar mb-16">
              <el-radio-group v-model="calendarDays" size="small" @change="loadCalendar">
                <el-radio-button :value="7">7天</el-radio-button>
                <el-radio-button :value="10">10天</el-radio-button>
                <el-radio-button :value="15">15天</el-radio-button>
                <el-radio-button :value="20">20天</el-radio-button>
              </el-radio-group>
            </div>

            <!-- 日历表格 -->
            <div class="calendar-table-wrapper" v-if="calendarData.dates?.length">
              <table class="calendar-table">
                <thead>
                  <tr>
                    <th class="sector-col">板块</th>
                    <th v-for="d in calendarData.dates" :key="d" class="date-col">
                      {{ formatDateShort(d) }}
                    </th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="sector in calendarData.sectors" :key="sector.code">
                    <td class="sector-cell">
                      <div class="sector-name">{{ sector.name }}</div>
                      <el-tag size="small" :type="sector.type === 'concept' ? 'danger' : 'warning'" effect="light">
                        {{ sector.type === 'concept' ? '概念' : '行业' }}
                      </el-tag>
                    </td>
                    <td
                      v-for="d in calendarData.dates"
                      :key="d"
                      class="state-cell"
                      :class="calendarCellClass(calendarData.matrix[sector.code]?.[d])"
                    >
                      <template v-if="calendarData.matrix[sector.code]?.[d]">
                        <div class="state-badge">{{ calendarStateLabel(calendarData.matrix[sector.code][d]) }}</div>
                        <div v-if="calendarData.leaders[sector.code]?.[d]?.length" class="leader-hint">
                          {{ calendarData.leaders[sector.code][d][0].name }}
                        </div>
                      </template>
                      <span v-else class="empty-cell">-</span>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <el-empty v-else description="暂无日历数据" />
          </template>
        </el-tab-pane>

        <!-- 主线追踪 -->
        <el-tab-pane label="主线追踪" name="mainline">
          <div v-if="loading" class="loading-container">
            <el-skeleton :rows="5" animated />
          </div>
          <template v-else>
            <div class="mainline-section">
              <h3 class="section-title">
                <el-icon :size="18" class="title-icon"><TrendCharts /></el-icon>
                当前主线 ({{ mainLines.active_count || 0 }})
                <el-tag type="danger" size="small" effect="light">四档口径</el-tag>
              </h3>

              <div class="mainline-grid" v-if="activeMainLines.length">
                <div
                  v-for="ml in activeMainLines"
                  :key="ml.sector_code"
                  class="mainline-card"
                  :class="'mainline-card-' + (ml.main_line_status || 'none')"
                >
                  <div class="card-header">
                    <div class="sector-info">
                      <span class="sector-name">{{ ml.sector_name }}</span>
                      <el-tag :type="ml.sector_type === 'concept' ? 'danger' : 'warning'" size="small" effect="light">
                        {{ ml.sector_type === 'concept' ? '概念' : '行业' }}
                      </el-tag>
                      <el-tag
                        v-if="ml.main_line_status && ml.main_line_status !== 'none'"
                        :type="mainLineTagType(ml.main_line_status)"
                        size="small"
                        effect="light"
                        class="mainline-status-chip"
                      >
                        {{ ml.main_line_label }}
                      </el-tag>
                    </div>
                    <div class="duration-badge">
                      <el-icon><Timer /></el-icon>
                      {{ ml.duration_days }}天
                    </div>
                  </div>
                  <div class="card-stats">
                    <div class="stat-item">
                      <span class="stat-label">最高板</span>
                      <span class="stat-value text-up">{{ ml.max_height }}板</span>
                    </div>
                    <div class="stat-item">
                      <span class="stat-label">总涨停</span>
                      <span class="stat-value">{{ ml.total_limit_up }}只</span>
                    </div>
                    <div class="stat-item">
                      <span class="stat-label">龙头股</span>
                      <span class="stat-value leader-name" v-if="ml.leader_stock">
                        {{ ml.leader_name }}
                        <el-tag type="danger" size="small" effect="light">{{ ml.leader_max_height }}板</el-tag>
                      </span>
                      <span v-else class="stat-value text-muted">-</span>
                    </div>
                  </div>
                </div>
              </div>
              <el-empty v-else description="暂无活跃主线" />

              <h3 class="section-title mt-24" v-if="endedMainLines.length">
                <el-icon :size="18" class="title-icon"><Warning /></el-icon>
                近期结束主线
                <el-tag type="info" size="small" effect="light">回避</el-tag>
              </h3>
              <div class="table-container" v-if="endedMainLines.length">
                <el-table
                  :data="endedMainLines"
                  stripe
                  size="small"
                >
                  <el-table-column prop="sector_name" label="板块" min-width="140" />
                  <el-table-column prop="duration_days" label="持续天数" width="100" align="center" />
                  <el-table-column prop="max_height" label="最高板" width="90" align="center" />
                  <el-table-column prop="end_reason" label="结束原因" min-width="180" />
                </el-table>
              </div>
            </div>
          </template>
        </el-tab-pane>

        <!-- 板块强弱 -->
        <el-tab-pane label="板块强弱" name="strength">
          <div v-if="loading" class="loading-container">
            <el-skeleton :rows="8" animated />
          </div>
          <template v-else>
            <v-chart v-if="strengthList.length" :option="strengthChartOption" style="height: 400px" autoresize />
            <div class="table-container mt-16">
              <el-table :data="strengthList" stripe size="small" empty-text="暂无板块强弱数据" class="strength-table">
                <el-table-column prop="rank" label="排名" width="60" align="center">
                  <template #default="{ row }">
                    <span :class="rankClass(row.rank)" class="rank-number">{{ row.rank }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="sector_name" label="板块" min-width="150">
                  <template #default="{ row }">
                    <div class="sector-name-cell">
                      <span class="sector-name">{{ row.sector_name }}</span>
                      <el-tag v-if="row.is_hot" type="danger" size="small" effect="light" class="hot-tag">热</el-tag>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column prop="strength_score" label="强度" width="80" align="center">
                  <template #default="{ row }">
                    <span :class="strengthClass(row.strength_score)" class="strength-score">{{ row.strength_score }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="change_pct" label="涨跌幅" width="100" align="right">
                  <template #default="{ row }">
                    <span :class="changeColorClass(row.change_pct)">{{ formatChange(row.change_pct) }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="fund_flow" label="板块资金净额(亿)" width="130" align="right">
                  <template #default="{ row }">
                    <span :class="changeColorClass(row.fund_flow)">{{ formatFundFlow(row.fund_flow) }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="limit_up_count" label="涨停" width="70" align="center">
                  <template #default="{ row }">
                    <span :class="{ 'text-up': row.limit_up_count > 0 }">{{ row.limit_up_count || 0 }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="consecutive_days" label="连涨" width="80" align="center">
                  <template #default="{ row }">
                    <el-tag v-if="row.consecutive_days >= 3" type="danger" size="small" effect="light">{{ row.consecutive_days }}天</el-tag>
                    <el-tag v-else-if="row.consecutive_days >= 1" type="warning" size="small" effect="light">{{ row.consecutive_days }}天</el-tag>
                    <span v-else class="text-muted">0</span>
                  </template>
                </el-table-column>
                <el-table-column prop="rank_change" label="排名变化" width="90" align="center">
                  <template #default="{ row }">
                    <span v-if="row.rank_change > 0" class="text-up">
                      <el-icon><ArrowUp /></el-icon>{{ Math.min(row.rank_change, 99) }}
                    </span>
                    <span v-else-if="row.rank_change < 0" class="text-down">
                      <el-icon><ArrowDown /></el-icon>{{ Math.min(Math.abs(row.rank_change), 99) }}
                    </span>
                    <span v-else class="text-muted">-</span>
                  </template>
                </el-table-column>
              </el-table>
            </div>
            <!-- 分页 -->
            <div class="pagination-wrap" v-if="strengthTotal > pageSize">
              <el-pagination
                v-model:current-page="currentPage"
                :page-size="pageSize"
                :total="strengthTotal"
                layout="prev, pager, next"
                size="small"
                @current-change="onPageChange"
              />
            </div>
          </template>
        </el-tab-pane>

        <!-- 板块轮动 -->
        <el-tab-pane label="板块轮动" name="rotation">
          <div v-if="loading" class="loading-container">
            <el-skeleton :rows="5" animated />
          </div>
          <template v-else>
            <el-alert 
              v-if="!rotationSignals.length" 
              title="暂无轮动信号" 
              description="轮动信号需要至少2天排名数据对比，且排名变化≥5位时才触发" 
              type="info" 
              :closable="false" 
              show-icon 
              class="mb-16" 
            />
            <div class="table-container">
              <el-table :data="rotationSignals" stripe size="small" empty-text="暂无轮动信号" class="rotation-table">
                <el-table-column prop="from_name" label="流出板块" min-width="130">
                  <template #default="{ row }">
                    <div class="sector-flow outflow">
                      <span :class="{ 'text-down': row.from_name !== '其他' }">{{ row.from_name }}</span>
                      <el-tag v-if="row.from_type" size="small" :type="sectorTagType(row.from_type)" effect="light" class="ml-8">
                        {{ row.from_type === 'concept' ? '概念' : '行业' }}
                      </el-tag>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column label="流向" width="70" align="center">
                  <template #default>
                    <span class="flow-arrow">
                      <el-icon :size="20" color="var(--claw-primary)"><Right /></el-icon>
                    </span>
                  </template>
                </el-table-column>
                <el-table-column prop="to_name" label="流入板块" min-width="130">
                  <template #default="{ row }">
                    <div class="sector-flow inflow">
                      <span :class="{ 'text-up': row.to_name !== '其他' }">{{ row.to_name }}</span>
                      <el-tag v-if="row.to_type" size="small" :type="sectorTagType(row.to_type)" effect="light" class="ml-8">
                        {{ row.to_type === 'concept' ? '概念' : '行业' }}
                      </el-tag>
                    </div>
                  </template>
                </el-table-column>
                <el-table-column prop="flow_amount" label="流向(亿)" width="110" align="right">
                  <template #default="{ row }">{{ formatFundFlow(row.flow_amount) }}</template>
                </el-table-column>
                <el-table-column prop="rotation_type" label="类型" width="100" align="center">
                  <template #default="{ row }">
                    <el-tag :type="row.rotation_type === 'sudden' ? 'danger' : 'info'" size="small" effect="light">
                      {{ row.rotation_type_label || (row.rotation_type === 'sudden' ? '突然切换' : '渐进轮动') }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="confidence" label="置信度" width="90" align="center">
                  <template #default="{ row }">
                    <span :class="confidenceClass(row.confidence)" class="confidence-value">
                      {{ (row.confidence * 100).toFixed(0) }}%
                    </span>
                  </template>
                </el-table-column>
              </el-table>
            </div>
          </template>
        </el-tab-pane>

        <!-- 板块K线 -->
        <el-tab-pane label="板块K线" name="kline">
          <div class="kline-toolbar mb-16">
            <el-select
              v-model="klineSectorCode"
              filterable
              remote
              :remote-method="searchSectors"
              :loading="klineSearchLoading"
              placeholder="搜索板块名称..."
              size="default"
              style="width: 280px"
              @change="onKlineSectorChange"
            >
              <el-option
                v-for="s in klineSectorOptions"
                :key="s.sector_code"
                :label="s.sector_name"
                :value="s.sector_code"
              >
                <span>{{ s.sector_name }}</span>
                <el-tag size="small" :type="s.sector_type === 'concept' ? 'danger' : 'warning'" effect="light" class="ml-8">
                  {{ s.sector_type === 'concept' ? '概念' : '行业' }}
                </el-tag>
              </el-option>
            </el-select>
            <el-radio-group v-model="klineDays" size="small" @change="loadKlineData" class="ml-16">
              <el-radio-button :value="30">30天</el-radio-button>
              <el-radio-button :value="60">60天</el-radio-button>
              <el-radio-button :value="120">120天</el-radio-button>
              <el-radio-button :value="250">250天</el-radio-button>
            </el-radio-group>
          </div>

          <div v-if="klineLoading" class="loading-container">
            <el-skeleton :rows="10" animated />
          </div>
          <template v-else>
            <el-empty v-if="!klineData.kline?.length && klineSectorCode" description="暂无K线数据" />
            <template v-if="klineData.kline?.length">
              <!-- K线摘要 -->
              <div class="kline-summary mb-16" v-if="klineData.latest">
                <div class="summary-item">
                  <span class="summary-label">{{ klineData.sector_info?.sector_name }}</span>
                  <span class="summary-value big" :class="klineData.latest.change_pct >= 0 ? 'text-up' : 'text-down'">
                    {{ klineData.latest.close?.toFixed(2) }}
                  </span>
                </div>
                <div class="summary-item">
                  <span class="summary-label">涨跌幅</span>
                  <span class="summary-value" :class="klineData.latest.change_pct >= 0 ? 'text-up' : 'text-down'">
                    {{ formatChange(klineData.latest.change_pct) }}
                  </span>
                </div>
                <div class="summary-item">
                  <span class="summary-label">最高</span>
                  <span class="summary-value">{{ klineData.latest.high?.toFixed(2) }}</span>
                </div>
                <div class="summary-item">
                  <span class="summary-label">最低</span>
                  <span class="summary-value">{{ klineData.latest.low?.toFixed(2) }}</span>
                </div>
                <div class="summary-item">
                  <span class="summary-label">成交量</span>
                  <span class="summary-value">{{ formatVolume(klineData.latest.volume) }}</span>
                </div>
              </div>

              <!-- K线图 -->
              <v-chart :option="klineChartOption" style="height: 480px" autoresize />
              <v-chart :option="klineMAOption" style="height: 280px; margin-top: 12px" autoresize />
            </template>

            <!-- 快速选择 -->
            <div v-if="!klineSectorCode" class="kline-quick-pick">
              <div class="quick-title">选择板块查看K线</div>
              <div class="quick-grid">
                <el-card
                  v-for="s in hotSectorsForKline"
                  :key="s.sector_code"
                  shadow="hover"
                  class="quick-card"
                  @click="klineSectorCode = s.sector_code; onKlineSectorChange()"
                >
                  <div class="quick-name">{{ s.sector_name }}</div>
                  <div class="quick-meta">
                    <el-tag :type="s.sector_type === 'concept' ? 'danger' : 'warning'" size="small" effect="light">
                      {{ s.sector_type === 'concept' ? '概念' : '行业' }}
                    </el-tag>
                    <span :class="s.change_pct >= 0 ? 'text-up' : 'text-down'">
                      {{ formatChange(s.change_pct) }}
                    </span>
                  </div>
                </el-card>
              </div>
            </div>
          </template>
        </el-tab-pane>
      </el-tabs>
    </div>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { Flag, TopRight, TrendCharts, Sort, BottomRight, Timer, MoonNight } from '@element-plus/icons-vue'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureKlineChartsRegistered } from '@/composables/echarts/kline'
import {
  getSectorStrength, getSectorRotation, getSectorCount,
  getSectorLifecycle, getSectorLifecycleCalendar, getSectorMainLines,
  getSectorKline
} from '@/api'
import { formatChange, changeColorClass } from '@/composables/useUtils'

const router = useRouter()

ensureKlineChartsRegistered()

const activeSubTab = ref('lifecycle')
const loading = ref(true)
const sectorCategory = ref('concept')
const strengthList = ref([])
const rotationSignals = ref([])
const sectorCountData = ref({ industry: 0, concept: 0 })
const typeStatsMap = ref({})

// 生命周期相关
const lifecycleList = ref([])
const lifecycleStats = ref(null)
const lifecycleStateFilter = ref('')
const lifecycleTotal = ref(0)
const lifecyclePage = ref(1)
const lifecycleLoading = ref(false)

// 轮动日历
const calendarData = ref({})
const calendarDays = ref(10)

// 主线追踪
const mainLines = ref({ items: [], active_count: 0 })

// 分页
const currentPage = ref(1)
const pageSize = 50
const strengthTotal = ref(0)
const loadedTabs = ref({
  lifecycle: false,
  calendar: false,
  mainline: false,
  strength: false,
  rotation: false,
})

const categoryLabel = computed(() => sectorCategory.value === 'concept' ? '概念' : '行业')
const sectorCount = computed(() => sectorCountData.value[sectorCategory.value] || 0)

const stats = computed(() => {
  const catStats = typeStatsMap.value[sectorCategory.value] || {}
  return {
    total: sectorCount.value,
    hot: catStats.hot || 0,
    totalFundFlow: catStats.total_fund_flow || 0,
    tradeDate: strengthList.value.length ? (strengthList.value[0].__tradeDate || '--') : '--',
  }
})

function formatFundFlow(val) {
  if (val == null || isNaN(val)) return '--'
  const abs = Math.abs(val)
  if (abs >= 100) return val.toFixed(1) + '亿'
  if (abs >= 1) return val.toFixed(2) + '亿'
  if (abs >= 0.01) return (val * 10000).toFixed(0) + '万'
  return val.toFixed(4) + '亿'
}

function rankClass(rank) {
  if (rank <= 3) return 'text-up rank-top'
  if (rank <= 10) return 'text-warning'
  return ''
}

function strengthClass(score) {
  if (score >= 80) return 'text-up'
  if (score >= 60) return 'text-warning'
  if (score >= 40) return ''
  return 'text-down'
}

function qualityClass(score) {
  if (score >= 80) return 'text-up'
  if (score >= 60) return 'text-warning'
  if (score >= 40) return ''
  return 'text-muted'
}

function confidenceClass(conf) {
  if (conf >= 0.8) return 'text-up'
  if (conf >= 0.5) return 'text-warning'
  return ''
}

function sectorTagType(rawType) {
  return rawType === 'concept' ? 'danger' : 'warning'
}

function stateTagType(state) {
  const map = {
    emerging: 'success',
    accelerating: 'primary',
    climax: 'danger',
    diverging: 'warning',
    declining: 'info',
    one_day: 'info',
    dormant: 'info',
    capital_probe: 'warning',
  }
  return map[state] || 'info'
}

function stateLabel(state) {
  const map = {
    emerging: '刚启动',
    accelerating: '加速',
    climax: '高潮',
    diverging: '分化',
    declining: '退潮',
    one_day: '一日游',
    dormant: '休眠',
    capital_probe: '资金试探',
  }
  return map[state] || state
}

function stateIcon(state) {
  const map = {
    emerging: Flag,
    accelerating: TopRight,
    climax: TrendCharts,
    diverging: Sort,
    declining: BottomRight,
    one_day: Timer,
    dormant: MoonNight,
    capital_probe: TrendCharts,
  }
  return map[state] || Flag
}

function stateDesc(state) {
  const map = {
    emerging: '首板出现(1-2只),资金初进,新热点萌芽,可关注但需观察持续性',
    accelerating: '连板梯队形成(>=3只),资金持续流入,板块加速上升,可积极参与',
    climax: '批量涨停(>=10只),龙头高度>5板,注意分化风险,谨慎追高',
    diverging: '从高潮/加速回落,掉队股增多,后排开始跌,注意止盈',
    declining: '板块达到高潮或加速后持续回调: 龙头断板/梯队瓦解/K线破位/资金大幅出逃, 坚决回避勿接飞刀',
    one_day: '当天涨停次日无持续,无溢价,避免追高,一日游板块',
    dormant: '无涨停,无资金关注,等待信号,暂不参与',
    capital_probe: '资金流入和趋势先改善，但涨停结构尚未形成，属于试探阶段',
  }
  return map[state] || state
}

function isCapitalProbe(row) {
  if (row.display_state === 'capital_probe') return true
  const fundFlow = Number(row.fund_flow || 0)
  const changePct = Number(row.change_pct || 0)
  const strength = Number(row.strength_score ?? row.state_score ?? 0)
  const trend = row.kline_trend
  const close = Number(row.kline_close || 0)
  const ma5 = Number(row.kline_ma5 || 0)
  const ma20 = Number(row.kline_ma20 || 0)
  const rawLimitUp = Number(row.raw_limit_up_count || 0)

  const weakBreakdown = trend === 'down' || trend === 'breakdown' || trend === 'breakout_down' || (close && ma20 && close < ma20 * 0.97)
  const trendImproving = trend === 'up' || trend === 'breakout_up' || (close && ma5 && close >= ma5)

  return row.lifecycle_state === 'dormant'
    && (row.limit_up_count || 0) === 0
    && fundFlow > 5
    && (changePct >= 1 || strength >= 45)
    && !weakBreakdown
    && (rawLimitUp > 0 || trendImproving || (close && ma5 && ma20 && close >= ma5 && ma5 >= ma20))
}

function displayStateKey(row) {
  return row.display_state || (isCapitalProbe(row) ? 'capital_probe' : row.lifecycle_state)
}

function displayStateLabel(row) {
  return row.display_state_label || stateLabel(displayStateKey(row))
}

function displayStateTagType(row) {
  return stateTagType(displayStateKey(row))
}

function attributionConfidenceHint(level) {
  const map = {
    primary: '主归因：原因词直接命中板块主题。',
    theme: '主题映射：行业词或主题词映射后归因。',
    resonance: '龙头共振：强板块按龙头结构兜底归因。',
  }
  return map[level] || ''
}

function stateJudgeHint(row) {
  const displayState = displayStateKey(row)
  const limitUp = row.limit_up_count || 0
  const firstBoard = row.first_board_count || 0
  const consecutiveBoard = row.consecutive_board_count || 0
  const maxBoard = row.max_board_height || 0
  const fundFlow = Number(row.fund_flow || 0)
  const strength = Number(row.strength_score ?? row.state_score ?? 0)

  const hints = {
    emerging: maxBoard >= 5 && consecutiveBoard <= 1
      ? '高标存在，但后排以首板为主，按宽度型启动处理。'
      : '涨停开始扩散，资金承接转强，处于启动观察阶段。',
    accelerating: '连板梯队已形成，龙头高度与资金共振，进入加速阶段。',
    climax: '批量涨停且高标高度突出，短线情绪处于高潮区间。',
    diverging: fundFlow < 0
      ? '高标或涨停结构仍在，但资金转弱，后排承接开始分化。'
      : '高标独强、后排跟随不足，板块内部结构开始分化。',
    declining: '高标断板或梯队坍塌，资金继续恶化，退潮信号明确。',
    one_day: '前一日启动后无持续承接，板块热度未能延续。',
    dormant: '缺少有效涨停结构与资金承接，暂不具备活跃信号。',
    capital_probe: '资金与趋势先改善，但涨停结构尚未成型，先按试探信号观察。',
  }

  if (displayState === 'diverging' && maxBoard >= 5 && firstBoard >= Math.max(limitUp - 1, 4)) {
    return '高标较高，但首板占比过高、后排跟不上，偏分化而非主升。'
  }
  if (displayState === 'accelerating' && limitUp >= 10 && maxBoard >= 4 && fundFlow > 0) {
    return '板块宽度与龙头高度同步抬升，属于爆发式加速。'
  }
  if (displayState === 'emerging' && strength >= 75 && consecutiveBoard < 3) {
    return '强度较高，但连板梯队尚未完全成型，先归为刚启动。'
  }

  return hints[displayState] || '依据涨停结构、资金强弱、最高板与梯队完整度综合判定。'
}

function klineJudgeHint(row) {
  const trend = row.kline_trend
  const close = Number(row.kline_close || 0)
  const ma5 = Number(row.kline_ma5 || 0)
  const ma20 = Number(row.kline_ma20 || 0)
  const volRatio = row.kline_vol_ratio != null ? Number(row.kline_vol_ratio) : null

  if (!trend && !close && !ma5 && !ma20) {
    return '暂无板块K线确认信号'
  }

  const tags = []
  if (trend === 'breakout_up') tags.push('向上突破')
  else if (trend === 'up') tags.push('趋势向上')
  else if (trend === 'sideways') tags.push('横盘震荡')
  else if (trend === 'down') tags.push('趋势转弱')
  else if (trend === 'breakdown' || trend === 'breakout_down') tags.push('破位下行')

  if (close && ma5 && ma20) {
    if (close >= ma5 && ma5 >= ma20) tags.push('站上MA5/MA20')
    else if (close >= ma5) tags.push('站上MA5')
    else if (close < ma20 * 0.97) tags.push('跌破MA20')
    else if (close < ma5) tags.push('跌破MA5')
  } else if (close && ma5) {
    tags.push(close >= ma5 ? '站上MA5' : '跌破MA5')
  }

  if (volRatio != null) {
    if (volRatio >= 1.2) tags.push(`放量${volRatio.toFixed(2)}`)
    else if (volRatio <= 0.8) tags.push(`缩量${volRatio.toFixed(2)}`)
  }

  return tags.length ? tags.join('，') : 'K线信号中性'
}

function hasLifecycleRawFallback(row) {
  return (row.limit_up_count || 0) === 0 && (row.raw_limit_up_count || 0) > 0
}

function displayLimitUpCount(row) {
  return row.limit_up_count || 0
}

function displayFirstBoardCount(row) {
  return row.first_board_count || 0
}

function displayConsecutiveBoardCount(row) {
  return row.consecutive_board_count || 0
}

function displayMaxBoardHeight(row) {
  return row.max_board_height || 0
}

function displayLeaderStocks(row) {
  return row.leader_stocks || []
}

function displayLadderStocks(row) {
  return row.ladder_stocks || []
}

function mainLineTagType(status) {
  const map = {
    strengthening: 'danger',
    continuing: 'warning',
    diverging: 'info',
  }
  return map[status] || 'info'
}

function heightClass(h) {
  if (h >= 5) return 'text-up font-bold'
  if (h >= 3) return 'text-warning font-bold'
  if (h >= 2) return 'text-warning'
  return 'text-muted'
}

function goStock(code) {
  if (!code) return
  router.push({ name: 'stock-detail', params: { code } })
}

function calendarCellClass(state) {
  if (!state) return ''
  const map = {
    emerging: 'cell-emerging',
    accelerating: 'cell-accelerating',
    climax: 'cell-climax',
    diverging: 'cell-diverging',
    declining: 'cell-declining',
    one_day: 'cell-oneday',
    dormant: 'cell-dormant',
  }
  return map[state] || ''
}

function calendarStateLabel(state) {
  const map = {
    emerging: '启动',
    accelerating: '加速',
    climax: '高潮',
    diverging: '分化',
    declining: '退潮',
    one_day: '一日游',
    dormant: '休眠',
  }
  return map[state] || state
}

function formatDateShort(dateStr) {
  const d = new Date(dateStr)
  return `${d.getMonth() + 1}/${d.getDate()}`
}

function formatVolume(val) {
  if (val == null || isNaN(val)) return '--'
  if (val >= 100000000) return (val / 100000000).toFixed(2) + '亿手'
  if (val >= 10000) return (val / 10000).toFixed(1) + '万手'
  return val.toFixed(0) + '手'
}

const strengthChartOption = computed(() => {
  const items = strengthList.value.slice(0, 15)
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      formatter: (params) => {
        const d = params[0]
        const item = items[items.length - 1 - d.dataIndex]
        if (!item) return ''
        return `<b>${item.sector_name}</b><br/>` +
          `强度: ${item.strength_score}<br/>` +
          `涨跌: ${formatChange(item.change_pct)}<br/>` +
          `资金流: ${formatFundFlow(item.fund_flow)}<br/>` +
          `涨停: ${item.limit_up_count || 0}只<br/>` +
          `连续: ${item.consecutive_days || 0}天`
      }
    },
    grid: { left: 140, right: 50, top: 10, bottom: 20 },
    xAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: 'var(--claw-border)' } },
      axisLabel: { color: 'var(--claw-text-muted)' },
      splitLine: { lineStyle: { color: 'var(--claw-border-light)' } },
    },
    yAxis: {
      type: 'category',
      data: items.map(i => i.sector_name).reverse(),
      axisLine: { lineStyle: { color: 'var(--claw-border)' } },
      axisLabel: { color: 'var(--claw-text-secondary)', fontSize: 11 },
    },
    series: [{
      type: 'bar',
      data: items.map(i => ({
        value: i.strength_score,
        itemStyle: { 
          color: i.strength_score >= 80 ? 'var(--claw-up)' : 
                 i.strength_score >= 60 ? 'var(--claw-warning)' : 
                 i.strength_score >= 40 ? 'var(--claw-primary)' : 'var(--claw-down)'
        }
      })).reverse(),
      barWidth: 14,
      label: { show: true, position: 'right', color: 'var(--claw-text-muted)', fontSize: 11 },
    }],
  }
})

function resetTabLoadedState() {
  loadedTabs.value = {
    lifecycle: false,
    calendar: false,
    mainline: false,
    strength: false,
    rotation: false,
  }
}

function resetTabData(tabName) {
  if (tabName === 'lifecycle') {
    lifecycleList.value = []
    lifecycleStats.value = null
    lifecycleTotal.value = 0
  } else if (tabName === 'calendar') {
    calendarData.value = {}
  } else if (tabName === 'mainline') {
    mainLines.value = { items: [], active_count: 0 }
  } else if (tabName === 'strength') {
    strengthList.value = []
    strengthTotal.value = 0
  } else if (tabName === 'rotation') {
    rotationSignals.value = []
  }
}

async function loadStrengthOverview(force = false) {
  if (!force && loadedTabs.value.strength) return
  const res = await getSectorStrength({
    sector_type: sectorCategory.value,
    page: currentPage.value,
    page_size: pageSize,
  })
  strengthList.value = res?.items || []
  strengthTotal.value = res?.total || 0
  const tradeDate = res?.trade_date || '--'
  strengthList.value.forEach(item => { item.__tradeDate = tradeDate })
  if (res?.type_stats) {
    typeStatsMap.value = res.type_stats
  }
  loadedTabs.value.strength = true
}

async function loadCurrentTabData(force = false) {
  const tab = activeSubTab.value
  if (!force && loadedTabs.value[tab]) return

  if (tab === 'lifecycle') {
    await loadLifecycle(force)
    return
  }

  if (tab === 'calendar') {
    const res = await getSectorLifecycleCalendar({
      days: calendarDays.value,
      sector_type: sectorCategory.value,
    })
    calendarData.value = res || {}
    loadedTabs.value.calendar = true
    return
  }

  if (tab === 'mainline') {
    const res = await getSectorMainLines({ sector_type: sectorCategory.value })
    mainLines.value = res || { items: [], active_count: 0 }
    loadedTabs.value.mainline = true
    return
  }

  if (tab === 'rotation') {
    const res = await getSectorRotation({ sector_type: sectorCategory.value })
    rotationSignals.value = res?.signals || []
    loadedTabs.value.rotation = true
    return
  }
}

async function loadLifecycle(force = false) {
  if (!force && loadedTabs.value.lifecycle) return

  const res = await getSectorLifecycle({
    sector_type: sectorCategory.value,
    state: lifecycleStateFilter.value || undefined,
    force_refresh: force || undefined,
    page: lifecyclePage.value,
    page_size: pageSize,
  })
  lifecycleList.value = res?.items || []
  lifecycleStats.value = res?.state_stats || null
  lifecycleTotal.value = res?.total || 0
  loadedTabs.value.lifecycle = true
}

async function refreshLifecycle(force = true) {
  lifecycleLoading.value = true
  try {
    await loadLifecycle(force)
  } catch (e) {
    console.error('生命周期数据加载失败:', e)
  } finally {
    lifecycleLoading.value = false
  }
}

async function loadData(force = false) {
  loading.value = true
  try {
    await Promise.all([
      loadStrengthOverview(force),
      loadCurrentTabData(force),
    ])
  } catch (e) {
    console.error('板块营地数据加载失败:', e)
  } finally {
    loading.value = false
  }
}

async function loadCount() {
  try {
    const res = await getSectorCount()
    sectorCountData.value = {
      industry: res.industry || 0,
      concept: res.concept || 0,
    }
  } catch (e) {
    console.error('板块数量获取失败:', e)
  }
}

function onCategoryChange() {
  currentPage.value = 1
  lifecyclePage.value = 1
  klineSectorCode.value = ''
  klineData.value = {}
  klineSectorOptions.value = []
  sectorSearchPool.value = []
  resetTabLoadedState()
  resetTabData('lifecycle')
  resetTabData('calendar')
  resetTabData('mainline')
  resetTabData('strength')
  resetTabData('rotation')
  loadData(true)
}

function onLifecycleFilterChange() {
  lifecyclePage.value = 1
  loadedTabs.value.lifecycle = false
  resetTabData('lifecycle')
  refreshLifecycle(true)
}

function onLifecyclePageChange() {
  loadedTabs.value.lifecycle = false
  resetTabData('lifecycle')
  refreshLifecycle(true)
}

function loadCalendar() {
  loadedTabs.value.calendar = false
  resetTabData('calendar')
  loadData(true)
}

function onPageChange() {
  loadedTabs.value.strength = false
  resetTabData('strength')
  loadData(true)
}

// K线相关
const klineSectorCode = ref('')
const klineDays = ref(60)
const klineLoading = ref(false)
const klineData = ref({})
const klineSearchLoading = ref(false)
const klineSectorOptions = ref([])
const sectorSearchPool = ref([])

const hotSectorsForKline = computed(() => {
  return strengthList.value.slice(0, 8).map(s => ({
    sector_code: s.sector_code,
    sector_name: s.sector_name,
    sector_type: sectorCategory.value,
    change_pct: s.change_pct,
  }))
})

const mainLinePriorityMap = {
  strengthening: 0,
  continuing: 1,
  diverging: 2,
  none: 9,
}

const activeMainLines = computed(() => {
  return [...(mainLines.value.items || [])]
    .filter(item => item.status === 'active')
    .sort((a, b) => {
      const priorityDiff = (mainLinePriorityMap[a.main_line_status || 'none'] ?? 9) - (mainLinePriorityMap[b.main_line_status || 'none'] ?? 9)
      if (priorityDiff !== 0) return priorityDiff
      if ((b.max_height || 0) !== (a.max_height || 0)) return (b.max_height || 0) - (a.max_height || 0)
      if ((b.duration_days || 0) !== (a.duration_days || 0)) return (b.duration_days || 0) - (a.duration_days || 0)
      return (b.avg_fund_flow || 0) - (a.avg_fund_flow || 0)
    })
})

const endedMainLines = computed(() => {
  return [...(mainLines.value.items || [])]
    .filter(item => item.status === 'ended')
    .sort((a, b) => (b.duration_days || 0) - (a.duration_days || 0))
})

async function searchSectors(query) {
  const keyword = (query || '').trim()
  klineSearchLoading.value = true
  try {
    if (!sectorSearchPool.value.length) {
      const firstPage = await getSectorStrength({
        sector_type: sectorCategory.value,
        page: 1,
        page_size: 200,
      })
      const total = firstPage?.total || 0
      const pages = Math.max(1, Math.ceil(total / 200))
      const allItems = [...(firstPage?.items || [])]

      for (let p = 2; p <= pages; p++) {
        const nextPage = await getSectorStrength({
          sector_type: sectorCategory.value,
          page: p,
          page_size: 200,
        })
        allItems.push(...(nextPage?.items || []))
      }
      sectorSearchPool.value = allItems
    }

    const pool = sectorSearchPool.value
    const filtered = keyword
      ? pool.filter(s => s.sector_name?.includes(keyword))
      : pool

    klineSectorOptions.value = filtered.slice(0, 50).map(s => ({
      sector_code: s.sector_code,
      sector_name: s.sector_name,
      sector_type: s.sector_type || sectorCategory.value,
    }))
  } finally {
    klineSearchLoading.value = false
  }
}

async function loadKlineData() {
  if (!klineSectorCode.value) return
  klineLoading.value = true
  try {
    const res = await getSectorKline({
      sector_code: klineSectorCode.value,
      days: klineDays.value,
    })
    klineData.value = res || {}
  } catch (e) {
    console.error('K线数据加载失败:', e)
    klineData.value = {}
  } finally {
    klineLoading.value = false
  }
}

function onKlineSectorChange() {
  loadKlineData()
}

const klineChartOption = computed(() => {
  const kline = klineData.value.kline || []
  if (!kline.length) return {}

  const dates = kline.map(k => k.trade_date)
  const ohlc = kline.map(k => [k.open, k.close, k.low, k.high])
  const volumes = kline.map(k => k.volume || 0)
  const changes = kline.map(k => k.change_pct || 0)

  const ma = klineData.value.ma || {}
  const ma5Map = Object.fromEntries((ma.ma5 || []).map(m => [m.trade_date, m.value]))
  const ma10Map = Object.fromEntries((ma.ma10 || []).map(m => [m.trade_date, m.value]))
  const ma20Map = Object.fromEntries((ma.ma20 || []).map(m => [m.trade_date, m.value]))
  const ma60Map = Object.fromEntries((ma.ma60 || []).map(m => [m.trade_date, m.value]))

  return {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'var(--claw-bg-card)',
      borderColor: 'var(--claw-border)',
      borderWidth: 1,
      textStyle: { color: 'var(--claw-text)', fontSize: 12 },
    },
    legend: {
      data: ['日K', 'MA5', 'MA10', 'MA20', 'MA60'],
      top: 0,
      textStyle: { color: 'var(--claw-text-muted)', fontSize: 11 },
      itemWidth: 12, itemHeight: 8,
    },
    grid: [
      { left: 70, right: 30, top: 40, height: '55%' },
      { left: 70, right: 30, top: '72%', height: '18%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { lineStyle: { color: 'var(--claw-border)' } } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 }, axisTick: { show: false }, axisLine: { lineStyle: { color: 'var(--claw-border)' } } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, scale: true, axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 }, splitLine: { lineStyle: { color: 'var(--claw-border-light)', type: 'dashed' } }, axisLine: { show: false } },
      { type: 'value', gridIndex: 1, axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10, formatter: v => v >= 10000 ? (v/10000).toFixed(0) + '万' : v }, splitLine: { lineStyle: { color: 'var(--claw-border-light)', type: 'dashed' } }, axisLine: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - 60 / kline.length * 100), end: 100 },
    ],
    series: [
      {
        name: '日K',
        type: 'candlestick',
        data: ohlc,
        xAxisIndex: 0,
        yAxisIndex: 0,
        itemStyle: {
          color: 'var(--claw-up)',
          color0: 'var(--claw-down)',
          borderColor: 'var(--claw-up)',
          borderColor0: 'var(--claw-down)',
        },
      },
      {
        name: 'MA5',
        type: 'line',
        data: dates.map(d => ma5Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#f7a528' },
      },
      {
        name: 'MA10',
        type: 'line',
        data: dates.map(d => ma10Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#4f6ef7' },
      },
      {
        name: 'MA20',
        type: 'line',
        data: dates.map(d => ma20Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#8b5cf6' },
      },
      {
        name: 'MA60',
        type: 'line',
        data: dates.map(d => ma60Map[d] ?? null),
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#0ea5e9' },
      },
      {
        name: '成交量',
        type: 'bar',
        data: volumes.map((v, i) => ({
          value: v,
          itemStyle: { color: (changes[i] || 0) >= 0 ? 'rgba(239,68,68,0.5)' : 'rgba(34,197,94,0.5)' }
        })),
        xAxisIndex: 1,
        yAxisIndex: 1,
      },
    ],
  }
})

const klineMAOption = computed(() => {
  const kline = klineData.value.kline || []
  if (!kline.length) return {}

  const dates = kline.map(k => k.trade_date)
  const ma = klineData.value.ma || {}
  const ma5Map = Object.fromEntries((ma.ma5 || []).map(m => [m.trade_date, m.value]))
  const ma10Map = Object.fromEntries((ma.ma10 || []).map(m => [m.trade_date, m.value]))
  const ma20Map = Object.fromEntries((ma.ma20 || []).map(m => [m.trade_date, m.value]))
  const ma60Map = Object.fromEntries((ma.ma60 || []).map(m => [m.trade_date, m.value]))

  return {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      backgroundColor: 'var(--claw-bg-card)',
      borderColor: 'var(--claw-border)',
      borderWidth: 1,
      textStyle: { color: 'var(--claw-text)', fontSize: 12 },
    },
    legend: {
      data: ['MA5', 'MA10', 'MA20', 'MA60'],
      top: 0,
      textStyle: { color: 'var(--claw-text-muted)', fontSize: 11 },
      itemWidth: 12, itemHeight: 8,
    },
    grid: { left: 70, right: 30, top: 30, bottom: 30 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: 'var(--claw-border)' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: 'var(--claw-text-muted)', fontSize: 10 },
      splitLine: { lineStyle: { color: 'var(--claw-border-light)', type: 'dashed' } },
      axisLine: { show: false },
    },
    dataZoom: [
      { type: 'inside', start: Math.max(0, 100 - 60 / kline.length * 100), end: 100 },
    ],
    series: [
      { name: 'MA5', type: 'line', data: dates.map(d => ma5Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#f7a528' } },
      { name: 'MA10', type: 'line', data: dates.map(d => ma10Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#4f6ef7' } },
      { name: 'MA20', type: 'line', data: dates.map(d => ma20Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#8b5cf6' } },
      { name: 'MA60', type: 'line', data: dates.map(d => ma60Map[d] ?? null), smooth: true, symbol: 'none', lineStyle: { width: 1.5, color: '#0ea5e9' } },
    ],
  }
})

onMounted(async () => {
  await loadCount()
  await loadData()
})

watch(activeSubTab, async (tab) => {
  if (tab === 'kline') return
  if (!loadedTabs.value[tab]) {
    resetTabData(tab)
    await loadData()
  }
})
</script>

<style scoped lang="scss">
.sectors-page {
  display: flex;
  flex-direction: column;
  gap: var(--spacing-4);
}

.title-icon {
  margin-right: var(--spacing-2);
}

.mb-16 {
  margin-bottom: var(--spacing-4);
}

.mt-16 {
  margin-top: var(--spacing-4);
}

.mt-24 {
  margin-top: var(--spacing-6);
}

.ml-8 {
  margin-left: var(--spacing-2);
}

.ml-16 {
  margin-left: var(--spacing-4);
}

.font-bold {
  font-weight: 700;
}

.cursor-pointer {
  cursor: pointer;
}

.state-reason-tooltip {
  max-width: 280px;
  line-height: 1.6;
  font-size: 12px;
}

.state-reason-title {
  font-size: 13px;
  font-weight: 700;
  margin-bottom: 4px;
}

.state-reason-line {
  color: rgba(255, 255, 255, 0.92);
}

.pagination-total {
  font-size: 0.8125rem;
  color: var(--claw-text-muted);
}

// 统计卡片
.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--spacing-4);

  @media (max-width: 1365px) {
    grid-template-columns: repeat(2, 1fr);
  }

  @media (max-width: 767px) {
    grid-template-columns: 1fr;
  }
}

.stat-card-v2 {
  display: flex;
  align-items: center;
  gap: var(--spacing-4);
  padding: var(--spacing-4);
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
  transition: all var(--transition-base);

  &:hover {
    box-shadow: var(--shadow-md);
    transform: translateY(-2px);
  }

  &.hot {
    border-color: var(--claw-border);
    background: linear-gradient(135deg, rgba(225, 82, 72, 0.06) 0%, var(--claw-bg-card) 100%);
  }

  &.inflow {
    border-color: var(--claw-border);
    background: linear-gradient(135deg, rgba(34, 197, 94, 0.06) 0%, var(--claw-bg-card) 100%);
  }

  &.outflow {
    border-color: var(--claw-border);
    background: linear-gradient(135deg, rgba(239, 68, 68, 0.05) 0%, var(--claw-bg-card) 100%);
  }

  .stat-icon-wrap {
    width: 48px;
    height: 48px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: var(--radius-md);
    background: var(--primary-50);
    color: var(--claw-primary);
  }

  .hot-icon {
    background: rgba(225, 82, 72, 0.1);
    color: #e15248;
  }

  .flow-icon {
    background: rgba(34, 197, 94, 0.1);
    color: #22c55e;
  }

  .stat-content {
    flex: 1;
  }

  .stat-label {
    font-size: 0.75rem;
    color: var(--claw-text-muted);
    margin-bottom: var(--spacing-1);
  }

  .stat-value {
    font-size: 1.5rem;
    font-weight: 700;
    font-variant-numeric: tabular-nums;

    &.date {
      font-size: 1.125rem;
    }
  }

  .stat-desc {
    font-size: 0.75rem;
    color: var(--claw-text-muted);
    margin-top: var(--spacing-1);
  }
}

// 分类标签
.category-tabs {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--spacing-4);
  flex-wrap: wrap;
  padding: 0 var(--spacing-1);
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;

  :deep(.el-radio-group) {
    display: inline-flex;
    gap: var(--spacing-2);
  }

  :deep(.el-radio-button__inner) {
    height: 40px;
    line-height: 38px;
    padding: 0 18px;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
}

.count-badge {
  font-size: 0.875rem;
  color: var(--claw-text-muted);
  font-weight: 500;
}

.sectors-metrics-panel {
  padding: var(--spacing-2) 0;
  background: transparent;
  border: 0;
  border-radius: 0;
  box-shadow: none;
}

// 子标签页
.subtabs-shell {
  background: transparent;
  border: 0;
  border-radius: 0;
  padding: var(--spacing-1) 0 0;
  box-shadow: none;

  :deep(.el-tabs__header) {
    margin: 0 0 var(--spacing-2);
  }

  :deep(.el-tabs__item) {
    height: 38px;
    line-height: 38px;
    padding: 0 14px;
    margin-right: var(--spacing-1);
    font-weight: 600;
  }

  :deep(.el-tabs__nav-wrap::after) {
    height: 1px;
  }

  :deep(.el-tabs__content) {
    padding-top: var(--spacing-2);
  }
}

// 状态筛选
.state-filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-1);
  margin-bottom: var(--spacing-3);
  padding-left: 2px;
  overflow: visible;

  :deep(.el-radio-group) {
    display: flex;
    flex-wrap: wrap;
    gap: var(--spacing-2);
    padding-left: 1px;
    overflow: visible;
  }

  :deep(.el-radio-button) {
    margin: 0 !important;
  }

  :deep(.el-radio-button__inner) {
    height: 27px;
    line-height: 25px;
    padding: 0 9px;
    font-size: 0.8125rem;
    font-weight: 500;
    border-radius: var(--radius-md);
    border: 0 !important;
    margin-left: 0 !important;
    box-shadow: none !important;
    color: var(--claw-text-secondary);
    background: rgba(255, 255, 255, 0.72);
  }

  :deep(.el-radio-button.is-active .el-radio-button__inner) {
    box-shadow: none !important;
    color: #fff !important;
    background: linear-gradient(135deg, #5ea2f6 0%, #4f8fe6 100%) !important;
  }

  /* 清理 Element Plus 连体按钮默认样式，避免左侧描边/拼接感 */
  :deep(.el-radio-button:first-child .el-radio-button__inner),
  :deep(.el-radio-button:last-child .el-radio-button__inner),
  :deep(.el-radio-button:first-child:last-child .el-radio-button__inner) {
    border-radius: var(--radius-md) !important;
    box-shadow: none !important;
  }

  :deep(.el-radio-button.is-active .el-radio-button__original-radio:not(:disabled)+.el-radio-button__inner) {
    box-shadow: none !important;
    color: #fff !important;
    background: linear-gradient(135deg, #5ea2f6 0%, #4f8fe6 100%) !important;
  }

  :deep(.el-radio-button__original-radio:focus-visible + .el-radio-button__inner) {
    outline: none !important;
    outline-offset: 0 !important;
    border-left: 0 !important;
    z-index: auto !important;
    box-shadow: none !important;
  }

  .state-pill-label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }

  .state-pill-icon {
    font-size: 13px;
    flex-shrink: 0;
  }

  .icon-emerging { color: #59c36a; }
  .icon-accelerating { color: #4f9df0; }
  .icon-climax { color: #e15248; }
  .icon-diverging { color: #f2bf3a; }
  .icon-declining { color: #8b97ac; }
  .icon-one-day { color: #c6ccd6; }
}

// 生命周期汇总栏
.lc-summary-bar {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-1);
  align-items: center;
  padding: 4px 8px;
  margin-top: 4px;
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: var(--radius-lg);
}

.lc-stat-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 8px;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--transition-fast);
  font-size: 0.65625rem;
  user-select: none;
  white-space: nowrap;
  background: var(--claw-bg-card);
  border: 1px solid rgba(148, 163, 184, 0.22);

  &:hover, &.active {
    transform: translateY(-0.5px);
    box-shadow: 0 1px 3px rgba(15, 23, 42, 0.07);
    border-color: var(--claw-primary);
  }

  &.active {
    font-weight: 600;
    background: var(--primary-50);
  }

  .lc-state-icon {
    font-size: 12px;
    display: inline-flex;
    flex-shrink: 0;
  }

  .lc-name {
    color: var(--claw-text-secondary);
  }

  b {
    color: var(--claw-text-primary);
    font-size: 0.75rem;
    min-width: 12px;
    text-align: center;
    font-variant-numeric: tabular-nums;
  }
}

.lc-info {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: rgba(59, 130, 246, 0.12);
  color: var(--claw-primary);
  border: 1px solid rgba(59, 130, 246, 0.24);
  font-size: 8px;
  font-weight: 700;
  font-style: normal;
  margin-left: 1px;
  opacity: 1;
  transition: all var(--transition-fast);
}

.lc-stat-item:hover .lc-info {
  background: rgba(59, 130, 246, 0.18);
  border-color: rgba(59, 130, 246, 0.36);
}

.lc-state-icon-emerging { color: var(--success-500); }
.lc-state-icon-accelerating { color: var(--claw-primary); }
.lc-state-icon-climax { color: var(--error-500); }
.lc-state-icon-diverging { color: var(--claw-warning); }
.lc-state-icon-declining { color: var(--neutral-500); }
.lc-state-icon-one_day { color: var(--neutral-400); }
.lc-state-icon-dormant { color: var(--neutral-300); }

.lifecycle-state-tag {
  :deep(.el-tag__content) {
    display: inline-flex;
    align-items: center;
  }
}

.state-tag-content {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.state-tag-icon {
  font-size: 12px;
}

.state-tag-icon-emerging { color: var(--success-500); }
.state-tag-icon-accelerating { color: var(--claw-primary); }
.state-tag-icon-climax { color: var(--error-500); }
.state-tag-icon-diverging { color: var(--claw-warning); }
.state-tag-icon-declining { color: var(--neutral-500); }
.state-tag-icon-one_day { color: var(--neutral-400); }
.state-tag-icon-dormant { color: var(--neutral-300); }

// 表格容器
.table-container {
  border-radius: var(--radius-lg);
  overflow: hidden;
  border: 1px solid var(--claw-border);
}

// 表格样式
:deep(.el-table) {
  border-radius: 0;
  border: none;

  th.el-table__cell {
    background: var(--neutral-50);
    font-weight: 600;
    color: var(--claw-text-secondary);
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.025em;
  }

  .el-table__row:hover > td {
    background: var(--primary-50);
  }
}

.lifecycle-table {
  :deep(.el-table__cell .cell) {
    white-space: nowrap;
  }
}

// 板块名称单元格
.sector-name-cell {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);

  .sector-name {
    font-weight: 500;
  }

  .main-line-tag,
  .hot-tag {
    font-size: 0.6875rem;
  }
}

// 涨停统计
.limit-up-stats {
  display: inline-flex;
  align-items: center;
  gap: var(--spacing-1);
  font-weight: 500;

  .separator {
    color: var(--claw-text-muted);
  }
}

// 连板高度
.board-height {
  font-weight: 600;
  padding: var(--spacing-1) var(--spacing-2);
  border-radius: var(--radius-sm);
  background: var(--neutral-50);
}

// 龙头股标签
.leader-tag {
  font-weight: 600;
  cursor: pointer;
  transition: all var(--transition-fast);
  white-space: nowrap;

  &:hover {
    transform: scale(1.05);
  }

  .leader-height {
    font-weight: 400;
    opacity: 0.8;
  }
}

// 排名
.rank-number {
  font-weight: 700;
  font-size: 1rem;

  &.rank-top {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    height: 24px;
    background: var(--error-100);
    border-radius: 50%;
  }
}

// 强度分数
.strength-score {
  font-weight: 700;
  font-size: 1rem;
}

// 连板梯队弹窗
.ladder-popover {
  max-height: 360px;
  overflow-y: auto;
  padding: var(--spacing-3);
}

.ladder-header {
  font-weight: 600;
  font-size: 0.875rem;
  margin-bottom: var(--spacing-3);
  color: var(--claw-text-primary);
  padding-bottom: var(--spacing-2);
  border-bottom: 1px solid var(--claw-border-light);
}

.ladder-row {
  display: flex;
  align-items: center;
  margin-bottom: var(--spacing-2);
  gap: var(--spacing-3);
}

.ladder-height {
  min-width: 48px;
  font-weight: 600;
  text-align: right;
  font-size: 0.875rem;
}

.ladder-stocks {
  display: flex;
  flex-wrap: wrap;
  gap: var(--spacing-1);

  .stock-tag {
    cursor: pointer;
    transition: all var(--transition-fast);

    &:hover {
      transform: scale(1.05);
    }
  }
}

// 日历表格
.calendar-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: var(--spacing-4);
}

.calendar-table-wrapper {
  overflow-x: auto;
  max-height: 600px;
  overflow-y: auto;
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);
}

.calendar-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.8125rem;

  th, td {
    border: 1px solid var(--claw-border-light);
    padding: var(--spacing-2);
    text-align: center;
  }

  th {
    background: var(--neutral-50);
    font-weight: 600;
    position: sticky;
    top: 0;
    z-index: 1;
    color: var(--claw-text-secondary);
  }

  .sector-col {
    min-width: 120px;
    position: sticky;
    left: 0;
    background: var(--claw-bg-card);
    z-index: 2;
  }

  .date-col {
    min-width: 60px;
  }

  .sector-cell {
    text-align: left;
    background: var(--claw-bg-card);
    position: sticky;
    left: 0;

    .sector-name {
      font-weight: 500;
      margin-bottom: var(--spacing-1);
    }
  }

  .state-cell {
    min-width: 60px;
    height: 50px;
    vertical-align: middle;

    .state-badge {
      font-size: 0.6875rem;
      padding: var(--spacing-1) var(--spacing-2);
      border-radius: var(--radius-sm);
      display: inline-block;
      font-weight: 500;
    }

    .leader-hint {
      font-size: 0.625rem;
      color: var(--claw-text-muted);
      margin-top: var(--spacing-1);
    }

    .empty-cell {
      color: var(--claw-text-muted);
    }
  }

  .cell-emerging { background: rgba(34, 197, 94, 0.12); .state-badge { background: rgba(34, 197, 94, 0.3); color: var(--success-700); } }
  .cell-accelerating { background: rgba(14, 165, 233, 0.12); .state-badge { background: rgba(14, 165, 233, 0.3); color: var(--primary-700); } }
  .cell-climax { background: rgba(239, 68, 68, 0.12); .state-badge { background: rgba(239, 68, 68, 0.3); color: var(--error-700); } }
  .cell-diverging { background: rgba(245, 158, 11, 0.12); .state-badge { background: rgba(245, 158, 11, 0.3); color: var(--warning-700); } }
  .cell-declining { background: rgba(100, 116, 139, 0.12); .state-badge { background: rgba(100, 116, 139, 0.3); color: var(--neutral-700); } }
  .cell-oneday { background: rgba(203, 213, 225, 0.12); .state-badge { background: rgba(203, 213, 225, 0.5); color: var(--neutral-600); } }
  .cell-dormant { background: transparent; .state-badge { background: var(--neutral-100); color: var(--neutral-500); } }
}

// 主线追踪
.mainline-section {
  .section-title {
    display: flex;
    align-items: center;
    gap: var(--spacing-2);
    font-size: 1.125rem;
    font-weight: 600;
    color: var(--claw-text-primary);
    margin-bottom: var(--spacing-4);

    .title-icon {
      color: var(--claw-primary);
    }
  }
}

.mainline-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--spacing-4);
  margin-bottom: var(--spacing-6);

  @media (max-width: 1919px) {
    grid-template-columns: repeat(2, 1fr);
  }

  @media (max-width: 1023px) {
    grid-template-columns: 1fr;
  }
}

.mainline-card {
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  padding: var(--spacing-5);
  box-shadow: var(--shadow-sm);
  transition: all var(--transition-base);

  &:hover {
    box-shadow: var(--shadow-md);
    transform: translateY(-2px);
  }

  &.mainline-card-strengthening {
    border-color: rgba(239, 68, 68, 0.28);
    box-shadow: 0 10px 24px rgba(239, 68, 68, 0.10);
    background: linear-gradient(180deg, rgba(255, 247, 247, 0.98) 0%, var(--claw-bg-card) 100%);
  }

  &.mainline-card-continuing {
    border-color: rgba(245, 158, 11, 0.22);
    box-shadow: 0 8px 18px rgba(245, 158, 11, 0.08);
    background: linear-gradient(180deg, rgba(255, 251, 235, 0.95) 0%, var(--claw-bg-card) 100%);
  }

  &.mainline-card-diverging {
    border-color: rgba(148, 163, 184, 0.22);
    box-shadow: none;
    background: linear-gradient(180deg, rgba(248, 250, 252, 0.92) 0%, var(--claw-bg-card) 100%);
    opacity: 0.88;
  }

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    margin-bottom: var(--spacing-4);

    .sector-info {
      display: flex;
      align-items: center;
      gap: var(--spacing-2);
    }

    .sector-name {
      font-size: 1.125rem;
      font-weight: 600;
      color: var(--claw-text-primary);
    }

    .duration-badge {
      display: flex;
      align-items: center;
      gap: var(--spacing-1);
      padding: var(--spacing-1) var(--spacing-2);
      background: var(--primary-50);
      color: var(--primary-700);
      border-radius: var(--radius-sm);
      font-size: 0.8125rem;
      font-weight: 500;
    }
  }

  .card-stats {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: var(--spacing-3);

    .stat-item {
      text-align: center;
      padding: var(--spacing-3);
      background: var(--neutral-50);
      border-radius: var(--radius-md);

      .stat-label {
        font-size: 0.75rem;
        color: var(--claw-text-muted);
        margin-bottom: var(--spacing-1);
      }

      .stat-value {
        font-size: 1.125rem;
        font-weight: 700;
        font-variant-numeric: tabular-nums;

        &.leader-name {
          display: flex;
          align-items: center;
          justify-content: center;
          gap: var(--spacing-1);
          font-size: 0.875rem;
        }
      }
    }
  }
}

.mainline-card-diverging {
  .duration-badge {
    background: var(--neutral-100);
    color: var(--neutral-600);
  }

  .stat-item {
    background: rgba(148, 163, 184, 0.06) !important;
  }

  .sector-name,
  .stat-value {
    color: var(--claw-text-secondary) !important;
  }
}

// 轮动表格
.sector-flow {
  display: flex;
  align-items: center;
  gap: var(--spacing-2);
  font-weight: 500;

  &.outflow {
    color: var(--claw-down);
  }

  &.inflow {
    color: var(--claw-up);
  }
}

.flow-arrow {
  display: inline-flex;
  align-items: center;
  justify-content: center;
}

.confidence-value {
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}

// K线工具栏
.kline-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--spacing-3);
  padding: var(--spacing-4);
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  margin-bottom: var(--spacing-4);
}

// K线摘要
.kline-summary {
  display: flex;
  gap: var(--spacing-6);
  flex-wrap: wrap;
  padding: var(--spacing-4) var(--spacing-5);
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: var(--radius-lg);

  .summary-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    min-width: 80px;

    .summary-label {
      font-size: 0.75rem;
      color: var(--claw-text-muted);
      margin-bottom: var(--spacing-1);
      font-weight: 500;
    }

    .summary-value {
      font-size: 1.125rem;
      font-weight: 600;
      font-variant-numeric: tabular-nums;

      &.big {
        font-size: 1.5rem;
      }
    }
  }
}

// 快速选择
.kline-quick-pick {
  padding: var(--spacing-5);
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: var(--radius-lg);
  box-shadow: var(--shadow-sm);

  .quick-title {
    font-size: 1rem;
    font-weight: 600;
    margin-bottom: var(--spacing-4);
    color: var(--claw-text-primary);
  }

  .quick-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
    gap: var(--spacing-3);
  }

  .quick-card {
    cursor: pointer;
    transition: all var(--transition-base);

    &:hover {
      transform: translateY(-2px);
      box-shadow: var(--shadow-md);
      border-color: var(--claw-primary);
    }

    :deep(.el-card__body) {
      padding: var(--spacing-4);
    }

    .quick-name {
      font-size: 1rem;
      font-weight: 600;
      margin-bottom: var(--spacing-3);
      color: var(--claw-text-primary);
    }

    .quick-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
  }
}

// 分页
.pagination-wrap {
  display: flex;
  justify-content: center;
  margin-top: var(--spacing-4);
}

.stats-fallback {
  opacity: 0.82;
}

.ladder-note {
  font-size: 0.75rem;
  color: var(--claw-text-muted);
  margin-bottom: var(--spacing-2);
}

// 加载状态
.loading-container {
  padding: var(--spacing-10) 0;
}

// 响应式
@media (max-width: 767px) {
  .sectors-page {
    gap: var(--spacing-3);
  }

  .state-filters {
    :deep(.el-radio-button__inner) {
      height: 26px;
      line-height: 24px;
      padding: 0 8px;
      font-size: 0.75rem;
    }
  }

  .category-tabs {
    flex-direction: column;
    align-items: stretch;

    :deep(.el-radio-button__inner) {
      height: 36px;
      line-height: 34px;
      padding: 0 12px;
    }
  }

  .subtabs-shell {
    :deep(.el-tabs__header) {
      margin-bottom: var(--spacing-2);
    }

    :deep(.el-tabs__item) {
      height: 34px;
      line-height: 34px;
      padding: 0 10px;
      margin-right: 0;
    }
  }

  .kline-toolbar {
    flex-direction: column;
    align-items: stretch;

    .el-select {
      width: 100% !important;
    }
  }

  .kline-summary {
    justify-content: center;
  }
}
</style>
