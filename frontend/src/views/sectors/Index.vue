<template>
  <div class="page-container">
    <h2 class="page-title">📊 板块营地</h2>

    <!-- 板块类型切换：概念/行业两个大Tab -->
    <div class="category-tabs mb-16">
      <el-radio-group v-model="sectorCategory" size="default" @change="onCategoryChange">
        <el-radio-button value="concept">🔥 概念板块</el-radio-button>
        <el-radio-button value="industry">🏭 行业板块</el-radio-button>
      </el-radio-group>
      <span class="count-badge">共 {{ sectorCount }} 个{{ categoryLabel }}</span>
    </div>

    <!-- 概览统计 -->
    <el-row :gutter="12" class="mb-16" v-if="stats.total > 0">
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">{{ categoryLabel }}总数</div>
          <div class="stat-value">{{ sectorCount }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">🔥 热门(资金流入>10亿)</div>
          <div class="stat-value text-red">{{ stats.hot }}</div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">💰 资金净流入</div>
          <div class="stat-value" :class="stats.totalFundFlow >= 0 ? 'text-red' : 'text-green'">
            {{ formatFundFlow(stats.totalFundFlow) }}
          </div>
        </el-card>
      </el-col>
      <el-col :xs="12" :sm="6">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">📅 数据日期</div>
          <div class="stat-value" style="font-size: 16px">{{ stats.tradeDate }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 子Tab: 强弱/轮动/持续性/生命周期 -->
    <el-tabs v-model="activeSubTab">
      <!-- 板块生命周期(核心重构) -->
      <el-tab-pane label="生命周期" name="lifecycle">
        <div v-if="loading" class="loading-container">
          <el-skeleton :rows="8" animated />
        </div>
        <template v-else>
          <!-- 状态筛选 -->
          <div class="state-filters mb-16">
            <el-radio-group v-model="lifecycleStateFilter" size="small" @change="onLifecycleFilterChange">
              <el-radio-button value="">全部</el-radio-button>
              <el-radio-button value="emerging">🚀 刚启动</el-radio-button>
              <el-radio-button value="accelerating">⚡ 加速</el-radio-button>
              <el-radio-button value="climax">🔴 高潮</el-radio-button>
              <el-radio-button value="diverging">🟡 分化</el-radio-button>
              <el-radio-button value="declining">🟣 退潮</el-radio-button>
              <el-radio-button value="one_day">⚪ 一日游</el-radio-button>
            </el-radio-group>
          </div>

          <!-- 状态汇总(一行紧凑，圆点+名称+数量) -->
          <div class="lc-summary-bar mb-8" v-if="lifecycleStats">
            <span class="lc-stat-item"
              v-for="(stat, state) in lifecycleStats"
              :key="state"
              @click="lifecycleStateFilter = state; onLifecycleFilterChange()"
              :class="{ active: lifecycleStateFilter === state }">
              <i class="lc-dot" :class="'lc-dot-' + state"></i>
              <span class="lc-name">{{ stateLabel(state).split(' ')[1] || state }}</span>
              <b>{{ stat.count }}</b>
              <el-tooltip placement="top">
                <template #content><div style="max-width:240px;line-height:1.6;font-size:13px">{{ stateDesc(state) }}</div></template>
                <i class="lc-info">?</i>
              </el-tooltip>
            </span>
          </div>

          <!-- 生命周期表格 -->
          <el-table :data="lifecycleList" stripe size="small" empty-text="暂无生命周期数据">
            <el-table-column prop="sector_name" label="板块" min-width="130">
              <template #default="{ row }">
                <span>{{ row.sector_name }}</span>
                <el-tag v-if="row.is_main_line" type="danger" size="small" class="ml-4">主线</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="state_label" label="状态" width="100" align="center">
              <template #default="{ row }">
                <el-tag :type="stateTagType(row.lifecycle_state)" size="small">
                  {{ row.state_label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="change_pct" label="涨跌" width="80" align="right">
              <template #default="{ row }">
                <span v-if="row.change_pct != null && row.change_pct != 0" :class="changeColorClass(row.change_pct)">{{ formatChange(row.change_pct) }}</span>
                <span v-else class="text-gray">-</span>
              </template>
            </el-table-column>
            <el-table-column prop="state_score" label="强度" width="70" align="center">
              <template #default="{ row }">
                <span :class="strengthClass(row.strength_score ?? row.state_score)">{{ row.strength_score != null ? Math.round(row.strength_score) : (row.state_score ?? '-') }}</span>
              </template>
            </el-table-column>
            <el-table-column label="涨停/首板/连板" width="140" align="center">
              <template #default="{ row }">
                <el-popover placement="right" :width="380" trigger="hover" v-if="row.ladder_stocks?.length">
                  <template #reference>
                    <span class="cursor-pointer">
                      <span class="text-red">{{ row.limit_up_count ?? '-' }}</span>
                      <span class="text-gray"> / </span>
                      <span>{{ row.first_board_count ?? '-' }}</span>
                      <span class="text-gray"> / </span>
                      <span class="text-orange">{{ row.consecutive_board_count ?? '-' }}</span>
                    </span>
                  </template>
                  <div class="ladder-popover">
                    <div class="ladder-header">连板梯队 (涨停{{ row.limit_up_count }}/首板{{ row.first_board_count }}/连板{{ row.consecutive_board_count }})</div>
                    <div v-for="ladder in row.ladder_stocks" :key="ladder.height" class="ladder-row">
                      <span class="ladder-height" :class="heightClass(ladder.height)">{{ ladder.height >= 2 ? ladder.height + '连板' : '首板' }}</span>
                      <span class="ladder-stocks">
                        <el-tag v-for="s in ladder.stocks.slice(0, 8)" :key="s.code" size="small" :type="ladder.height >= 3 ? 'danger' : 'warning'" class="mr-4 cursor-pointer" @click="goStock(s.code)">
                          {{ s.name }}
                        </el-tag>
                        <span v-if="ladder.stocks.length > 8" class="text-gray">+{{ ladder.stocks.length - 8 }}</span>
                      </span>
                    </div>
                  </div>
                </el-popover>
                <template v-else>
                  <span class="text-red">{{ row.limit_up_count ?? '-' }}</span>
                  <span class="text-gray"> / </span>
                  <span>{{ row.first_board_count ?? '-' }}</span>
                  <span class="text-gray"> / </span>
                  <span class="text-orange">{{ row.consecutive_board_count ?? '-' }}</span>
                </template>
              </template>
            </el-table-column>
            <el-table-column prop="max_board_height" label="最高板" width="90" align="center">
              <template #default="{ row }">
                <template v-if="row.max_board_height > 0 && row.leader_stocks?.length">
                  <span :class="heightClass(row.max_board_height)" class="cursor-pointer" @click="goStock(row.leader_stocks[0].code)">
                    {{ row.max_board_height >= 2 ? row.max_board_height + '连板' : '首板' }}
                  </span>
                </template>
                <span v-else-if="row.max_board_height > 0" :class="heightClass(row.max_board_height)">{{ row.max_board_height >= 2 ? row.max_board_height + '连板' : '首板' }}</span>
                <span v-else class="text-gray">-</span>
              </template>
            </el-table-column>
            <el-table-column label="龙头股" width="150" align="center">
              <template #default="{ row }">
                <template v-if="row.leader_stocks && row.leader_stocks.length > 0">
                  <el-tag size="default" type="danger" class="leader-tag cursor-pointer" @click="goStock(row.leader_stocks[0].code)">
                    {{ row.leader_stocks[0].name }}({{ row.leader_stocks[0].height >= 2 ? row.leader_stocks[0].height + '连板' : '首板' }})
                  </el-tag>
                </template>
                <span v-else class="text-gray">-</span>
              </template>
            </el-table-column>
            <el-table-column prop="fund_flow" label="资金流(亿)" width="100" align="right">
              <template #default="{ row }">
                <span v-if="row.fund_flow != null && row.fund_flow !== ''" :class="changeColorClass(row.fund_flow)">
                  {{ formatFundFlow(row.fund_flow) }}
                </span>
                <span v-else class="text-gray">-</span>
              </template>
            </el-table-column>
            <el-table-column prop="quality_score" label="质量分" width="80" align="center">
              <template #default="{ row }">
                <span v-if="row.quality_score != null && row.quality_score > 0">{{ Math.round(row.quality_score) }}</span>
                <span v-else class="text-gray">-</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </el-tab-pane>

      <!-- 板块轮动日历(取代桑基图) -->
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
                    <el-tag size="small" :type="sector.type === 'concept' ? 'danger' : 'warning'">
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
          <h3 class="section-title">
            🔥 当前主线 ({{ mainLines.active_count || 0 }})
            <el-tag type="danger" size="small">积极参与</el-tag>
          </h3>

          <el-row :gutter="16" v-if="mainLines.items?.length">
            <el-col
              v-for="ml in mainLines.items.filter(i => i.status === 'active')"
              :key="ml.sector_code"
              :xs="24"
              :sm="12"
              :md="8"
              class="mb-16"
            >
              <el-card shadow="hover" class="mainline-card">
                <div class="card-header">
                  <span class="sector-name">{{ ml.sector_name }}</span>
                  <el-tag :type="ml.sector_type === 'concept' ? 'danger' : 'warning'" size="small">
                    {{ ml.sector_type === 'concept' ? '概念' : '行业' }}
                  </el-tag>
                </div>
                <div class="card-stats">
                  <div class="stat-item">
                    <span class="stat-label">持续</span>
                    <span class="stat-value">{{ ml.duration_days }}天</span>
                  </div>
                  <div class="stat-item">
                    <span class="stat-label">最高板</span>
                    <span class="stat-value text-red">{{ ml.max_height }}板</span>
                  </div>
                  <div class="stat-item">
                    <span class="stat-label">总涨停</span>
                    <span class="stat-value">{{ ml.total_limit_up }}只</span>
                  </div>
                </div>
                <div class="leader-section" v-if="ml.leader_stock">
                  <div class="leader-label">龙头股</div>
                  <div class="leader-info">
                    <span class="leader-name">{{ ml.leader_name }}</span>
                    <span class="leader-code">{{ ml.leader_stock }}</span>
                    <el-tag type="danger" size="small">{{ ml.leader_max_height }}板</el-tag>
                  </div>
                </div>
              </el-card>
            </el-col>
          </el-row>

          <h3 class="section-title mt-24" v-if="mainLines.items?.some(i => i.status === 'ended')">
            📉 近期结束主线
            <el-tag type="info" size="small">回避</el-tag>
          </h3>
          <el-table
            :data="mainLines.items?.filter(i => i.status === 'ended')"
            stripe
            size="small"
            v-if="mainLines.items?.some(i => i.status === 'ended')"
          >
            <el-table-column prop="sector_name" label="板块" min-width="120" />
            <el-table-column prop="duration_days" label="持续天数" width="90" align="center" />
            <el-table-column prop="max_height" label="最高板" width="80" align="center" />
            <el-table-column prop="end_reason" label="结束原因" min-width="150" />
          </el-table>
        </template>
      </el-tab-pane>

      <!-- 板块强弱(含资金流) -->
      <el-tab-pane label="板块强弱" name="strength">
        <div v-if="loading" class="loading-container">
          <el-skeleton :rows="8" animated />
        </div>
        <template v-else>
          <v-chart v-if="strengthList.length" :option="strengthChartOption" style="height: 400px" autoresize />
          <el-table :data="strengthList" stripe size="small" class="mt-16" empty-text="暂无板块强弱数据">
            <el-table-column prop="rank" label="排名" width="55" align="center">
              <template #default="{ row }">
                <span :class="rankClass(row.rank)">{{ row.rank }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="sector_name" label="板块" min-width="130">
              <template #default="{ row }">
                <span>{{ row.sector_name }}</span>
                <el-tag v-if="row.is_hot" type="danger" size="small" class="ml-4">🔥</el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="strength_score" label="强度" width="65" align="center">
              <template #default="{ row }">
                <span :class="strengthClass(row.strength_score)">{{ row.strength_score }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="change_pct" label="涨跌幅" width="90" align="right">
              <template #default="{ row }">
                <span :class="changeColorClass(row.change_pct)">{{ formatChange(row.change_pct) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="fund_flow" label="资金流(亿)" width="100" align="right">
              <template #default="{ row }">
                <span :class="changeColorClass(row.fund_flow)">{{ formatFundFlow(row.fund_flow) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="limit_up_count" label="涨停" width="50" align="center">
              <template #default="{ row }">
                <span :class="{ 'text-red': row.limit_up_count > 0 }">{{ row.limit_up_count || 0 }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="consecutive_days" label="连涨" width="55" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.consecutive_days >= 3" type="danger" size="small">{{ row.consecutive_days }}天</el-tag>
                <el-tag v-else-if="row.consecutive_days >= 1" type="warning" size="small">{{ row.consecutive_days }}天</el-tag>
                <span v-else class="text-gray">0</span>
              </template>
            </el-table-column>
            <el-table-column prop="stock_count" label="成分股" width="60" align="center">
              <template #default="{ row }">
                <span class="text-gray">{{ row.stock_count || '-' }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="rank_change" label="排名变化" width="70" align="center">
              <template #default="{ row }">
                <span v-if="row.rank_change > 0" class="text-red">↑{{ Math.min(row.rank_change, 99) }}</span>
                <span v-else-if="row.rank_change < 0" class="text-green">↓{{ Math.min(Math.abs(row.rank_change), 99) }}</span>
                <span v-else class="text-gray">-</span>
              </template>
            </el-table-column>
          </el-table>
          <!-- 分页 -->
          <div class="pagination-wrap" v-if="strengthTotal > pageSize">
            <el-pagination
              v-model:current-page="currentPage"
              :page-size="pageSize"
              :total="strengthTotal"
              layout="prev, pager, next"
              small
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
          <el-alert v-if="!rotationSignals.length" title="暂无轮动信号" description="轮动信号需要至少2天排名数据对比，且排名变化≥5位时才触发" type="info" :closable="false" show-icon class="mb-16" />
          <el-table :data="rotationSignals" stripe size="small" empty-text="暂无轮动信号">
            <el-table-column prop="from_name" label="流出板块" min-width="110">
              <template #default="{ row }">
                <span :class="{ 'text-green': row.from_name !== '其他' }">{{ row.from_name }}</span>
                <el-tag v-if="row.from_type" size="small" :type="sectorTagType(row.from_type)" class="ml-4">
                  {{ row.from_type === 'concept' ? '概念' : '行业' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="流向" width="60" align="center">
              <template #default><span class="text-yellow" style="font-size: 18px">→</span></template>
            </el-table-column>
            <el-table-column prop="to_name" label="流入板块" min-width="110">
              <template #default="{ row }">
                <span :class="{ 'text-red': row.to_name !== '其他' }">{{ row.to_name }}</span>
                <el-tag v-if="row.to_type" size="small" :type="sectorTagType(row.to_type)" class="ml-4">
                  {{ row.to_type === 'concept' ? '概念' : '行业' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="flow_amount" label="流向(亿)" width="100" align="right">
              <template #default="{ row }">{{ formatFundFlow(row.flow_amount) }}</template>
            </el-table-column>
            <el-table-column prop="rotation_type" label="类型" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.rotation_type === 'sudden' ? 'danger' : 'info'" size="small">
                  {{ row.rotation_type_label || (row.rotation_type === 'sudden' ? '突然切换' : '渐进轮动') }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="confidence" label="置信度" width="80" align="center">
              <template #default="{ row }">
                <span :class="confidenceClass(row.confidence)">{{ (row.confidence * 100).toFixed(0) }}%</span>
              </template>
            </el-table-column>
          </el-table>
        </template>
      </el-tab-pane>

      <!-- 板块持续性(含资金流) -->
      <el-tab-pane label="板块持续性" name="persistence">
        <div v-if="loading" class="loading-container">
          <el-skeleton :rows="6" animated />
        </div>
        <template v-else>
          <el-alert v-if="!persistenceList.length" title="暂无持续性数据" description="持续性分析需要板块连续活跃天数≥2天的数据" type="info" :closable="false" show-icon class="mb-16" />
          <el-table :data="persistenceList" stripe size="small" empty-text="暂无持续性数据">
            <el-table-column prop="sector_name" label="板块" min-width="130">
              <template #default="{ row }">
                <span>{{ row.sector_name }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="consecutive_days" label="连续天数" width="90" align="center">
              <template #default="{ row }">
                <el-tag :type="row.consecutive_days >= 5 ? 'danger' : row.consecutive_days >= 3 ? 'warning' : 'info'" size="small">
                  {{ row.consecutive_days }}天
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="limit_up_count" label="涨停数" width="70" align="center">
              <template #default="{ row }">
                <span :class="{ 'text-red': row.limit_up_count > 0 }">{{ row.limit_up_count }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="fund_flow" label="资金流(亿)" width="100" align="right">
              <template #default="{ row }">
                <span :class="changeColorClass(row.fund_flow)">{{ formatFundFlow(row.fund_flow) }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="strength_score" label="强度评分" width="80" align="center">
              <template #default="{ row }">
                <span :class="strengthClass(row.strength_score)">{{ row.strength_score }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="is_declining" label="状态" width="80" align="center">
              <template #default="{ row }">
                <el-tag :type="row.is_declining ? 'warning' : 'success'" size="small">
                  {{ row.is_declining ? '衰退⚠️' : '强势✅' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <!-- 分页 -->
          <div class="pagination-wrap" v-if="persistenceTotal > pageSize">
            <el-pagination
              v-model:current-page="persistencePage"
              :page-size="pageSize"
              :total="persistenceTotal"
              layout="prev, pager, next"
              small
              @current-change="onPersistencePageChange"
            />
          </div>
        </template>
      </el-tab-pane>

      <!-- 板块K线(趋势分析) -->
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
            style="width: 260px"
            @change="onKlineSectorChange"
          >
            <el-option
              v-for="s in klineSectorOptions"
              :key="s.sector_code"
              :label="s.sector_name"
              :value="s.sector_code"
            >
              <span>{{ s.sector_name }}</span>
              <el-tag size="small" :type="s.sector_type === 'concept' ? 'danger' : 'warning'" class="ml-8">
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

        <!-- K线图 -->
        <div v-if="klineLoading" class="loading-container">
          <el-skeleton :rows="10" animated />
        </div>
        <template v-else>
          <el-empty v-if="!klineData.kline?.length && klineSectorCode" description="暂无K线数据，请先运行采集脚本" />
          <template v-if="klineData.kline?.length">
            <!-- 最新K线摘要 -->
            <div class="kline-summary mb-16" v-if="klineData.latest">
              <div class="summary-item">
                <span class="summary-label">{{ klineData.sector_info?.sector_name }}</span>
                <span class="summary-value" :class="klineData.latest.change_pct >= 0 ? 'text-red' : 'text-green'">
                  {{ klineData.latest.close?.toFixed(2) }}
                </span>
              </div>
              <div class="summary-item">
                <span class="summary-label">涨跌幅</span>
                <span class="summary-value" :class="klineData.latest.change_pct >= 0 ? 'text-red' : 'text-green'">
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
              <div class="summary-item">
                <span class="summary-label">数据区间</span>
                <span class="summary-value text-gray">{{ klineData.sector_info?.data_range }} ({{ klineData.sector_info?.data_count }}日)</span>
              </div>
            </div>

            <!-- K线图+成交量 -->
            <v-chart :option="klineChartOption" style="height: 480px" autoresize />

            <!-- 均线趋势图 -->
            <v-chart :option="klineMAOption" style="height: 280px; margin-top: 12px" autoresize />
          </template>

          <!-- 快速选择热门板块 -->
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
                  <el-tag :type="s.sector_type === 'concept' ? 'danger' : 'warning'" size="small">
                    {{ s.sector_type === 'concept' ? '概念' : '行业' }}
                  </el-tag>
                  <span :class="s.change_pct >= 0 ? 'text-red' : 'text-green'">
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
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureEChartsRegistered } from '@/composables/echarts'
ensureEChartsRegistered()
import {
  getSectorStrength, getSectorRotation, getSectorPersistence, getSectorCount,
  getSectorLifecycle, getSectorLifecycleCalendar, getSectorMainLines,
  getSectorKline
} from '@/api'
import { formatChange, changeColorClass } from '@/composables/useUtils'

const router = useRouter()

const activeSubTab = ref('lifecycle')  // 默认显示生命周期Tab
const loading = ref(true)
const sectorCategory = ref('concept')
const strengthList = ref([])
const rotationSignals = ref([])
const persistenceList = ref([])
const sectorCountData = ref({ industry: 0, concept: 0 })
const typeStatsMap = ref({})  // 后端返回的全量统计 { concept: {total, hot, has_fund_in, total_fund_flow}, industry: {...} }

// 生命周期相关
const lifecycleList = ref([])
const lifecycleStats = ref(null)
const lifecycleStateFilter = ref('')
const lifecycleTotal = ref(0)

// 轮动日历
const calendarData = ref({})
const calendarDays = ref(10)

// 主线追踪
const mainLines = ref({ items: [], active_count: 0 })

// 分页
const currentPage = ref(1)
const pageSize = 50
const strengthTotal = ref(0)
const persistencePage = ref(1)
const persistenceTotal = ref(0)

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

/** 格式化资金流(亿) */
function formatFundFlow(val) {
  if (val == null || isNaN(val)) return '--'
  const abs = Math.abs(val)
  if (abs >= 100) return val.toFixed(1) + '亿'
  if (abs >= 1) return val.toFixed(2) + '亿'
  if (abs >= 0.01) return (val * 10000).toFixed(0) + '万'
  return val.toFixed(4) + '亿'
}

function rankClass(rank) {
  if (rank <= 3) return 'text-red'
  if (rank <= 10) return 'text-orange'
  return ''
}

function strengthClass(score) {
  if (score >= 80) return 'text-red'
  if (score >= 60) return 'text-orange'
  if (score >= 40) return ''
  return 'text-green'
}

function confidenceClass(conf) {
  if (conf >= 0.8) return 'text-red'
  if (conf >= 0.5) return 'text-orange'
  return ''
}

function sectorTagType(rawType) {
  return rawType === 'concept' ? 'danger' : 'warning'
}

// ========== 生命周期相关函数 ==========

function stateTagType(state) {
  const map = {
    emerging: 'success',      // 刚启动 - 绿色
    accelerating: 'primary',  // 加速 - 蓝色
    climax: 'danger',         // 高潮 - 红色
    diverging: 'warning',     // 分化 - 橙色
    declining: 'info',        // 退潮 - 灰色
    one_day: '',              // 一日游 - 默认
    dormant: 'info',          // 休眠 - 灰色
  }
  return map[state] || ''
}

function stateLabel(state) {
  const map = {
    emerging: '🚀 刚启动',
    accelerating: '⚡ 加速',
    climax: '🔴 高潮',
    diverging: '🟡 分化',
    declining: '🟣 退潮',
    one_day: '⚪ 一日游',
    dormant: '⚫ 休眠',
  }
  return map[state] || state
}

/** 状态详细说明(用于tooltip) */
function stateDesc(state) {
  const map = {
    emerging: '首板出现(1-2只),资金初进,新热点萌芽,可关注但需观察持续性',
    accelerating: '连板梯队形成(>=3只),资金持续流入,板块加速上升,可积极参与',
    climax: '批量涨停(>=10只),龙头高度>5板,注意分化风险,谨慎追高',
    diverging: '从高潮/加速回落,掉队股增多,后排开始跌,注意止盈',
    declining: '板块达到高潮或加速后持续回调: 龙头断板/梯队瓦解/K线破位/资金大幅出逃, 坚决回避勿接飞刀',
    one_day: '当天涨停次日无持续,无溢价,避免追高,一日游板块',
    dormant: '无涨停,无资金关注,等待信号,暂不参与',
  }
  return map[state] || state
}

function stateClass(state) {
  const map = {
    emerging: 'state-emerging',
    accelerating: 'state-accelerating',
    climax: 'state-climax',
    diverging: 'state-diverging',
    declining: 'state-declining',
    one_day: 'state-oneday',
    dormant: 'state-dormant',
  }
  return map[state] || ''
}

function heightClass(h) {
  if (h >= 5) return 'text-red font-bold'
  if (h >= 3) return 'text-orange font-bold'
  if (h >= 2) return 'text-orange'
  return 'text-gray'
}

/** 跳转到个股详情 */
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
          `连续: ${item.consecutive_days || 0}天<br/>` +
          `成分股: ${item.stock_count || '-'}只`
      }
    },
    grid: { left: 140, right: 50, top: 10, bottom: 20 },
    xAxis: {
      type: 'value',
      axisLine: { lineStyle: { color: '#e8ebf0' } },
      axisLabel: { color: '#667085' },
      splitLine: { lineStyle: { color: '#f0f2f5' } },
    },
    yAxis: {
      type: 'category',
      data: items.map(i => i.sector_name).reverse(),
      axisLine: { lineStyle: { color: '#e8ebf0' } },
      axisLabel: { color: '#667085', fontSize: 11 },
    },
    series: [{
      type: 'bar',
      data: items.map(i => ({
        value: i.strength_score,
        itemStyle: { color: i.strength_score >= 80 ? '#ef4444' : i.strength_score >= 60 ? '#f97316' : i.strength_score >= 40 ? '#3b82f6' : '#22c55e' }
      })).reverse(),
      barWidth: 14,
      label: { show: true, position: 'right', color: '#667085', fontSize: 11 },
    }],
  }
})

/** 加载数据(按当前类型) */
async function loadData() {
  loading.value = true
  try {
    const params = { sector_type: sectorCategory.value }

    const [s, r, p, l, c, m] = await Promise.allSettled([
      getSectorStrength({ ...params, page: currentPage.value, page_size: pageSize }),
      getSectorRotation(params),
      getSectorPersistence({ ...params, min_days: 1, page: persistencePage.value, page_size: pageSize }),
      getSectorLifecycle({ ...params, state: lifecycleStateFilter.value || undefined }),
      getSectorLifecycleCalendar({ days: calendarDays.value, ...params }),
      getSectorMainLines({ ...params }),
    ])

    if (s.status === 'fulfilled' && s.value) {
      strengthList.value = s.value.items || []
      strengthTotal.value = s.value.total || 0
      const tradeDate = s.value.trade_date || '--'
      strengthList.value.forEach(item => { item.__tradeDate = tradeDate })
      if (s.value.type_stats) {
        typeStatsMap.value = s.value.type_stats
      }
    }

    if (r.status === 'fulfilled' && r.value) {
      rotationSignals.value = r.value.signals || []
    }

    if (p.status === 'fulfilled' && p.value) {
      persistenceList.value = p.value.items || []
      persistenceTotal.value = p.value.total || 0
    }

    if (l.status === 'fulfilled' && l.value) {
      lifecycleList.value = l.value.items || []
      lifecycleStats.value = l.value.state_stats || null
      lifecycleTotal.value = l.value.total || 0
    }

    if (c.status === 'fulfilled' && c.value) {
      calendarData.value = c.value || {}
    }

    if (m.status === 'fulfilled' && m.value) {
      mainLines.value = m.value || { items: [], active_count: 0 }
    }
  } catch (e) {
    console.error('板块营地数据加载失败:', e)
  } finally {
    loading.value = false
  }
}

/** 加载板块数量 */
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
  persistencePage.value = 1
  // 切换板块类型时重置K线上下文，避免类型不一致的旧选择残留
  klineSectorCode.value = ''
  klineData.value = {}
  klineSectorOptions.value = []
  sectorSearchPool.value = []
  loadData()
}

function onLifecycleFilterChange() {
  loadData()
}

function loadCalendar() {
  loadData()
}

function onPageChange() {
  loadData()
}

function onPersistencePageChange() {
  loadPersistence()
}

async function loadPersistence() {
  try {
    const res = await getSectorPersistence({
      sector_type: sectorCategory.value,
      min_days: 1,
      page: persistencePage.value,
      page_size: pageSize,
    })
    persistenceList.value = res.items || []
    persistenceTotal.value = res.total || 0
  } catch (e) {
    console.error('持续性数据加载失败:', e)
  }
}

// ========== K线相关 ==========
const klineSectorCode = ref('')
const klineDays = ref(60)
const klineLoading = ref(false)
const klineData = ref({})
const klineSearchLoading = ref(false)
const klineSectorOptions = ref([])
const sectorSearchPool = ref([])

// 热门板块快速选择(从强弱列表取前8)
const hotSectorsForKline = computed(() => {
  return strengthList.value.slice(0, 8).map(s => ({
    sector_code: s.sector_code,
    sector_name: s.sector_name,
    sector_type: sectorCategory.value,
    change_pct: s.change_pct,
  }))
})

/** 搜索板块(用于K线选择器) */
async function searchSectors(query) {
  const keyword = (query || '').trim()
  klineSearchLoading.value = true
  try {
    // 首次搜索时拉取当前分类全量板块池，避免仅搜索当前分页的50条
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

/** 加载K线数据 */
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

/** 格式化成交量 */
function formatVolume(val) {
  if (val == null || isNaN(val)) return '--'
  if (val >= 100000000) return (val / 100000000).toFixed(2) + '亿手'
  if (val >= 10000) return (val / 10000).toFixed(1) + '万手'
  return val.toFixed(0) + '手'
}

/** K线图配置 */
const klineChartOption = computed(() => {
  const kline = klineData.value.kline || []
  if (!kline.length) return {}

  const dates = kline.map(k => k.trade_date)
  const ohlc = kline.map(k => [k.open, k.close, k.low, k.high])
  const volumes = kline.map(k => k.volume || 0)
  const changes = kline.map(k => k.change_pct || 0)

  // MA数据(从API返回的)
  const ma = klineData.value.ma || {}
  const ma5Data = (ma.ma5 || []).map(m => m.value)
  const ma10Data = (ma.ma10 || []).map(m => m.value)
  const ma20Data = (ma.ma20 || []).map(m => m.value)
  const ma60Data = (ma.ma60 || []).map(m => m.value)

  // 对齐MA到dates(用null填充前面的)
  function alignMA(maArr) {
    const result = []
    let j = 0
    for (let i = 0; i < dates.length; i++) {
      if (j < maArr.length && maArr[j] !== undefined) {
        // MA的date可能比kline少几个
        result.push(maArr[j])
        j++
      } else {
        result.push(null)
      }
    }
    return result
  }

  // 直接从kline数据与ma数据的trade_date对齐
  const ma5Map = Object.fromEntries((ma.ma5 || []).map(m => [m.trade_date, m.value]))
  const ma10Map = Object.fromEntries((ma.ma10 || []).map(m => [m.trade_date, m.value]))
  const ma20Map = Object.fromEntries((ma.ma20 || []).map(m => [m.trade_date, m.value]))
  const ma60Map = Object.fromEntries((ma.ma60 || []).map(m => [m.trade_date, m.value]))

  const ma5Aligned = dates.map(d => ma5Map[d] ?? null)
  const ma10Aligned = dates.map(d => ma10Map[d] ?? null)
  const ma20Aligned = dates.map(d => ma20Map[d] ?? null)
  const ma60Aligned = dates.map(d => ma60Map[d] ?? null)

  return {
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: '#fff',
      borderColor: '#e8ebf0',
      borderWidth: 1,
      textStyle: { color: '#1d2939', fontSize: 12 },
      formatter: (params) => {
        if (!params || !params.length) return ''
        const idx = params[0].dataIndex
        const k = kline[idx]
        if (!k) return ''
        let html = `<b>${k.trade_date}</b><br/>`
        html += `开: ${k.open?.toFixed(2)} 收: ${k.close?.toFixed(2)}<br/>`
        html += `高: ${k.high?.toFixed(2)} 低: ${k.low?.toFixed(2)}<br/>`
        html += `涨跌: <span style="color:${k.change_pct >= 0 ? '#ef4444' : '#22c55e'}">${formatChange(k.change_pct)}</span><br/>`
        html += `振幅: ${k.amplitude?.toFixed(2) || '-'}%<br/>`
        html += `成交量: ${formatVolume(k.volume)}`
        return html
      }
    },
    legend: {
      data: ['日K', 'MA5', 'MA10', 'MA20', 'MA60'],
      top: 0,
      textStyle: { color: '#667085', fontSize: 11 },
      itemWidth: 12, itemHeight: 8,
    },
    grid: [
      { left: 70, right: 30, top: 40, height: '55%' },
      { left: 70, right: 30, top: '72%', height: '18%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { show: false }, axisTick: { show: false }, axisLine: { lineStyle: { color: '#e8ebf0' } } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { color: '#667085', fontSize: 10 }, axisTick: { show: false }, axisLine: { lineStyle: { color: '#e8ebf0' } } },
    ],
    yAxis: [
      { type: 'value', gridIndex: 0, scale: true, axisLabel: { color: '#667085', fontSize: 10 }, splitLine: { lineStyle: { color: '#f0f2f5', type: 'dashed' } }, axisLine: { show: false } },
      { type: 'value', gridIndex: 1, axisLabel: { color: '#667085', fontSize: 10, formatter: v => v >= 10000 ? (v/10000).toFixed(0) + '万' : v }, splitLine: { lineStyle: { color: '#f0f2f5', type: 'dashed' } }, axisLine: { show: false } },
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
          color: '#ef4444',         // 涨-填充色(红)
          color0: '#22c55e',        // 跌-填充色(绿)
          borderColor: '#ef4444',   // 涨-边框(红)
          borderColor0: '#22c55e',  // 跌-边框(绿)
        },
      },
      {
        name: 'MA5',
        type: 'line',
        data: ma5Aligned,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#f7a528' },
      },
      {
        name: 'MA10',
        type: 'line',
        data: ma10Aligned,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#4f6ef7' },
      },
      {
        name: 'MA20',
        type: 'line',
        data: ma20Aligned,
        xAxisIndex: 0,
        yAxisIndex: 0,
        smooth: true,
        symbol: 'none',
        lineStyle: { width: 1, color: '#8b5cf6' },
      },
      {
        name: 'MA60',
        type: 'line',
        data: ma60Aligned,
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

/** 均线趋势图配置 */
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
      backgroundColor: '#fff',
      borderColor: '#e8ebf0',
      borderWidth: 1,
      textStyle: { color: '#1d2939', fontSize: 12 },
    },
    legend: {
      data: ['MA5', 'MA10', 'MA20', 'MA60'],
      top: 0,
      textStyle: { color: '#667085', fontSize: 11 },
      itemWidth: 12, itemHeight: 8,
    },
    grid: { left: 70, right: 30, top: 30, bottom: 30 },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { color: '#667085', fontSize: 10 },
      axisTick: { show: false },
      axisLine: { lineStyle: { color: '#e8ebf0' } },
    },
    yAxis: {
      type: 'value',
      scale: true,
      axisLabel: { color: '#667085', fontSize: 10 },
      splitLine: { lineStyle: { color: '#f0f2f5', type: 'dashed' } },
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
</script>

<style scoped lang="scss">
.mb-16 { margin-bottom: 16px; }
.mt-16 { margin-top: 16px; }
.ml-4 { margin-left: 4px; }
.ml-16 { margin-left: 16px; }

.text-red { color: #ef4444; }
.text-green { color: #22c55e; }
.text-orange { color: #f97316; }
.text-gray { color: #98a2b3; }
.text-yellow { color: #eab308; }
.font-bold { font-weight: bold; }
.mr-4 { margin-right: 4px; }
.mt-24 { margin-top: 24px; }

.stat-card {
  text-align: center;
  .stat-label {
    font-size: 12px;
    color: #98a2b3;
    margin-bottom: 4px;
  }
  .stat-value {
    font-size: 24px;
    font-weight: 700;
    color: #1d2939;
  }
}

.loading-container {
  padding: 20px;
}

.category-tabs {
  display: flex;
  align-items: center;
  gap: 16px;

  :deep(.el-radio-button__inner) {
    font-size: 14px;
    font-weight: 500;
  }
}

.count-badge {
  font-size: 13px;
  color: #98a2b3;
  font-weight: 500;
}

.pagination-wrap {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}

// 生命周期状态样式
.state-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* 一行状态汇总栏 - 单行内联，圆点+名称+数量+? */
.lc-summary-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
  padding: 8px 12px;
  background: var(--el-fill-color-lighter);
  border-radius: 8px;
}

.lc-stat-item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 14px;
  cursor: pointer;
  transition: all 0.15s;
  font-size: 12px;
  user-select: none;
  white-space: nowrap;

  &:hover, &.active {
    transform: translateY(-1px);
    box-shadow: 0 2px 6px rgba(0,0,0,0.1);
    background: var(--el-bg-color) !important;
  }
  &.active { font-weight: bold; }

  .lc-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
  .lc-name { color: var(--el-text-color-secondary); }
  b { color: var(--el-text-color-primary); font-size: 13px; min-width: 16px; text-align: center; }
}

/* 状态说明?号图标 */
.lc-info {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px; height: 14px;
  border-radius: 50%;
  background: var(--el-border-color);
  color: #fff;
  font-size: 9px;
  font-style: normal;
  margin-left: 2px;
  opacity: 0.5;
  transition: opacity 0.15s;
}
.lc-stat-item:hover .lc-info { opacity: 1; }

/* 圆点颜色 */
.lc-dot-emerging { background: #22c55e; }
.lc-dot-accelerating { background: #3b82f6; }
.lc-dot-climax { background: #ef4444; }
.lc-dot-diverging { background: #f59e0b; }
.lc-dot-declining { background: #8b5cf6; }
.lc-dot-one_day { background: #94a3b8; }
.lc-dot-dormant { background: #d1d5db; }

.leader-tag {
  font-weight: 600;
  cursor: pointer;
  transition: transform 0.15s, opacity 0.15s;
}
.leader-tag:hover {
  transform: scale(1.05);
  opacity: 0.85;
}

.cursor-pointer {
  cursor: pointer;
}
.cursor-pointer:hover {
  opacity: 0.75;
}

.ladder-popover {
  max-height: 360px;
  overflow-y: auto;
}
.ladder-header {
  font-weight: bold;
  font-size: 14px;
  margin-bottom: 8px;
  color: #e5e7eb;
}
.ladder-row {
  display: flex;
  align-items: center;
  margin-bottom: 6px;
  gap: 8px;
}
.ladder-height {
  min-width: 36px;
  font-weight: bold;
  text-align: right;
}
.ladder-stocks {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.state-emerging { border-left: 4px solid #22c55e; }
.state-accelerating { border-left: 4px solid #3b82f6; }
.state-climax { border-left: 4px solid #ef4444; }
.state-diverging { border-left: 4px solid #f59e0b; }
.state-declining { border-left: 4px solid #6b7280; }
.state-oneday { border-left: 4px solid #d1d5db; }
.state-dormant { border-left: 4px solid #9ca3af; }

// 日历表格样式
.calendar-toolbar {
  display: flex;
  justify-content: flex-end;
}

.calendar-table-wrapper {
  overflow-x: auto;
  max-height: 600px;
  overflow-y: auto;
}

.calendar-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;

  th, td {
    border: 1px solid var(--el-border-color);
    padding: 8px;
    text-align: center;
  }

  th {
    background: var(--el-fill-color-light);
    font-weight: 500;
    position: sticky;
    top: 0;
    z-index: 1;
  }

  .sector-col {
    min-width: 120px;
    position: sticky;
    left: 0;
    background: var(--el-bg-color);
    z-index: 2;
  }

  .date-col {
    min-width: 60px;
  }

  .sector-cell {
    text-align: left;
    background: var(--el-bg-color);

    .sector-name {
      font-weight: 500;
      margin-bottom: 4px;
    }
  }

  .state-cell {
    min-width: 60px;
    height: 50px;
    vertical-align: middle;

    .state-badge {
      font-size: 11px;
      padding: 2px 6px;
      border-radius: 4px;
      display: inline-block;
    }

    .leader-hint {
      font-size: 10px;
      color: var(--el-text-color-secondary);
      margin-top: 2px;
    }

    .empty-cell {
      color: var(--el-text-color-placeholder);
    }
  }

  // 状态颜色
  .cell-emerging { background: rgba(34, 197, 94, 0.15); .state-badge { background: rgba(34, 197, 94, 0.3); } }
  .cell-accelerating { background: rgba(59, 130, 246, 0.15); .state-badge { background: rgba(59, 130, 246, 0.3); } }
  .cell-climax { background: rgba(239, 68, 68, 0.15); .state-badge { background: rgba(239, 68, 68, 0.3); } }
  .cell-diverging { background: rgba(245, 158, 11, 0.15); .state-badge { background: rgba(245, 158, 11, 0.3); } }
  .cell-declining { background: rgba(107, 114, 128, 0.15); .state-badge { background: rgba(107, 114, 128, 0.3); } }
  .cell-oneday { background: rgba(209, 213, 219, 0.15); .state-badge { background: rgba(209, 213, 219, 0.3); } }
  .cell-dormant { background: transparent; .state-badge { background: var(--el-fill-color); } }
}

// 主线卡片样式
.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  margin: 0 0 16px 0;
}

.mainline-card {
  height: 100%;

  .card-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;

    .sector-name {
      font-size: 16px;
      font-weight: 500;
    }
  }

  .card-stats {
    display: flex;
    gap: 16px;
    margin-bottom: 12px;
    padding-bottom: 12px;
    border-bottom: 1px solid var(--el-border-color-lighter);

    .stat-item {
      display: flex;
      flex-direction: column;
      align-items: center;

      .stat-label {
        font-size: 12px;
        color: var(--el-text-color-secondary);
      }

      .stat-value {
        font-size: 16px;
        font-weight: 500;
        margin-top: 4px;
      }
    }
  }

  .leader-section {
    .leader-label {
      font-size: 12px;
      color: var(--el-text-color-secondary);
      margin-bottom: 4px;
    }

    .leader-info {
      display: flex;
      align-items: center;
      gap: 8px;

      .leader-name {
        font-weight: 500;
      }

      .leader-code {
        font-size: 12px;
        color: var(--el-text-color-secondary);
      }
    }
  }
}

// K线相关样式
.kline-toolbar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}

.ml-8 {
  margin-left: 8px;
}

.kline-summary {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
  padding: 12px 16px;
  background: var(--el-fill-color-light);
  border-radius: 8px;

  .summary-item {
    display: flex;
    flex-direction: column;
    align-items: center;

    .summary-label {
      font-size: 12px;
      color: var(--el-text-color-secondary);
      margin-bottom: 2px;
    }

    .summary-value {
      font-size: 16px;
      font-weight: 600;
    }
  }
}

.kline-quick-pick {
  .quick-title {
    font-size: 16px;
    font-weight: 500;
    margin-bottom: 16px;
    color: var(--el-text-color-secondary);
  }

  .quick-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
  }

  .quick-card {
    cursor: pointer;
    transition: all 0.2s;

    &:hover {
      transform: translateY(-2px);
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
    }

    .quick-name {
      font-size: 14px;
      font-weight: 500;
      margin-bottom: 8px;
    }

    .quick-meta {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
  }
}
</style>
