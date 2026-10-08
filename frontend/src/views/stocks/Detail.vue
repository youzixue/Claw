<template>
  <div class="page-container stock-detail-page">
    <section class="stock-hero">
      <div class="detail-context">
        <router-link to="/stocks" class="back-link">← 返回个股中心</router-link>
        <span class="context-divider"></span>
        <span>个股详情</span>
        <span v-if="spot.source_quote_at || spot.updated_at" class="quote-status">
          行情已更新
        </span>
      </div>

      <div class="stock-header">
        <div class="stock-info">
          <div class="stock-title-row">
            <h2>{{ displayStockName }}</h2>
            <span class="stock-code">{{ code }}</span>
            <el-tag v-if="tag" size="small" :type="tagType" effect="light">
              {{ stockTagLabel(tag) }}
            </el-tag>
          </div>

          <div v-if="spot.price" class="price-block">
            <div class="current-price" :class="changeColorClass(spot.change_pct)">
              {{ spot.price }}
            </div>
            <div class="price-change" :class="changeColorClass(spot.change_pct)">
              {{ formatChange(spot.change_pct) }}
            </div>
            <span class="price-caption">最新价</span>
          </div>
          <div v-else class="price-placeholder">正在加载行情…</div>
        </div>

        <div v-if="spot.price" class="market-snapshot">
          <div class="snapshot-item">
            <span>今开</span>
            <strong>{{ spot.open ?? '--' }}</strong>
          </div>
          <div class="snapshot-item">
            <span>最高</span>
            <strong :class="changeColorClass((spot.high || 0) - (spot.prev_close || 0))">{{ spot.high ?? '--' }}</strong>
          </div>
          <div class="snapshot-item">
            <span>最低</span>
            <strong :class="changeColorClass((spot.low || 0) - (spot.prev_close || 0))">{{ spot.low ?? '--' }}</strong>
          </div>
          <div class="snapshot-item">
            <span>昨收</span>
            <strong>{{ spot.prev_close ?? '--' }}</strong>
          </div>
        </div>

        <div class="score-badges" v-if="score.bull">
          <div class="badge">
            <div class="badge-head">
              <span>短线评分</span>
              <el-tag
                :color="levelColor(score.bull.level)"
                size="small"
                effect="dark"
                class="score-level"
              >
                {{ score.bull.level }}
              </el-tag>
            </div>
            <div class="badge-val" :style="{ color: levelColor(score.bull.level) }">
              {{ score.bull.total_score }}
            </div>
            <div class="badge-foot">综合强度</div>
          </div>
          <div class="badge" v-if="score.tenbagger">
            <div class="badge-head">
              <span>十倍潜力</span>
              <el-tag
                :color="levelColor(score.tenbagger.level)"
                size="small"
                effect="dark"
                class="score-level"
              >
                {{ score.tenbagger.level }}
              </el-tag>
            </div>
            <div class="badge-val" :style="{ color: levelColor(score.tenbagger.level) }">
              {{ score.tenbagger.total_score }}
            </div>
            <div class="badge-foot">成长评分</div>
          </div>
        </div>
      </div>

      <div v-if="spot.price" class="hero-metrics">
        <div class="hero-metric">
          <span>换手率</span>
          <strong>{{ spot.turnover != null ? spot.turnover + '%' : '--' }}</strong>
        </div>
        <div class="hero-metric">
          <span>量比</span>
          <strong>{{ spot.volume_ratio ?? '--' }}</strong>
        </div>
        <div class="hero-metric">
          <span>成交额</span>
          <strong>{{ spot.amount != null ? formatAmount(spot.amount) : '--' }}</strong>
        </div>
        <div class="hero-metric">
          <span>流通市值</span>
          <strong>{{ spot.circ_market_cap != null ? spot.circ_market_cap.toFixed(1) + '亿' : '--' }}</strong>
        </div>
        <div class="hero-metric">
          <span>PE(TTM)</span>
          <strong>{{ spot.pe_ttm ?? '--' }}</strong>
        </div>
        <div class="hero-metric">
          <span>净利润增速</span>
          <strong :class="changeColorClass(spot.net_profit_growth)">
            {{ spot.net_profit_growth != null ? formatChange(spot.net_profit_growth) : '--' }}
          </strong>
        </div>
      </div>
    </section>

    <el-tabs v-model="activeTab" class="detail-tabs">
      <!-- Tab 1: 概况(实时27字段) -->
      <el-tab-pane label="概况" name="profile">
        <div class="card-grid">
          <el-card shadow="never">
            <div class="section-title">
              实时行情
              <span class="section-note">资金字段为参考口径</span>
            </div>
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="最新价">{{ spot.price || '--' }}</el-descriptions-item>
              <el-descriptions-item label="昨收">{{ spot.prev_close || '--' }}</el-descriptions-item>
              <el-descriptions-item label="开盘">{{ spot.open || '--' }}</el-descriptions-item>
              <el-descriptions-item label="最高">{{ spot.high || '--' }}</el-descriptions-item>
              <el-descriptions-item label="最低">{{ spot.low || '--' }}</el-descriptions-item>
              <el-descriptions-item label="涨停">{{ spot.limit_up || '--' }}</el-descriptions-item>
              <el-descriptions-item label="跌停">{{ spot.limit_down || '--' }}</el-descriptions-item>
              <el-descriptions-item label="涨跌幅"><span :class="changeColorClass(spot.change_pct)">{{ formatChange(spot.change_pct) }}</span></el-descriptions-item>
              <el-descriptions-item label="振幅">{{ spot.amplitude ? spot.amplitude + '%' : '--' }}</el-descriptions-item>
              <el-descriptions-item label="5分钟涨跌"><span :class="changeColorClass(spot.min5_change)">{{ formatChange(spot.min5_change) }}</span></el-descriptions-item>
              <el-descriptions-item label="成交量">{{ spot.volume ? (spot.volume / 1e4).toFixed(2) + '万手' : '--' }}</el-descriptions-item>
              <el-descriptions-item label="成交额">{{ spot.amount ? (spot.amount / 1e8).toFixed(2) + '亿' : '--' }}</el-descriptions-item>
              <el-descriptions-item label="换手率">{{ spot.turnover ? spot.turnover + '%' : '--' }}</el-descriptions-item>
              <el-descriptions-item label="量比">{{ spot.volume_ratio || '--' }}</el-descriptions-item>
              <el-descriptions-item label="即时主力净额"><span class="spot-fund-value" :class="changeColorClass(spot.main_net_inflow)" :title="spotFundHint">{{ spot.main_fund_status === 'ok' && Number.isFinite(spot.main_net_inflow) ? (spot.main_net_inflow / 1e8).toFixed(2) + '亿' : '--' }}</span><span class="stock-meta spot-fund-status"> · {{ spotFundStatusLabel }}</span></el-descriptions-item>
              <el-descriptions-item label="委比">{{ spot.bid_ratio ? spot.bid_ratio + '%' : '--' }}</el-descriptions-item>
              <el-descriptions-item label="流通市值">{{ spot.circ_market_cap ? spot.circ_market_cap.toFixed(1) + '亿' : '--' }}</el-descriptions-item>
              <el-descriptions-item label="PE(TTM)">{{ spot.pe_ttm || '--' }}</el-descriptions-item>
              <el-descriptions-item label="PB">{{ spot.pb || '--' }}</el-descriptions-item>
              <el-descriptions-item label="股息率">{{ spot.dividend_yield ? spot.dividend_yield + '%' : '--' }}</el-descriptions-item>
              <el-descriptions-item label="净利润增速"><span :class="changeColorClass(spot.net_profit_growth)">{{ spot.net_profit_growth ? formatChange(spot.net_profit_growth) : '--' }}</span></el-descriptions-item>
              <el-descriptions-item label="均价VWAP">{{ spot.avg_price || '--' }}</el-descriptions-item>
            </el-descriptions>
          </el-card>

          <el-card shadow="never" v-if="hasOrderbook">
            <div class="section-title">盘口确认</div>
            <div class="orderbook-summary">
              <el-tag size="small" effect="light" :type="supportStrengthType">
                承接强度 {{ supportStrengthDisplay }}
              </el-tag>
              <el-tag size="small" effect="light" :type="sealQualityType">
                封单质量 {{ sealQualityDisplay }}
              </el-tag>
              <el-tag size="small" effect="light" :type="withdrawalRatioType">
                买盘撤单 {{ withdrawalRatioDisplay }}
              </el-tag>
              <el-tag size="small" effect="light" :type="imbalanceType">
                盘口失衡 {{ orderbookImbalanceDisplay }}
              </el-tag>
            </div>
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="买一">{{ formatOrderbookLevel(spot.orderbook?.bid1_price, spot.orderbook?.bid1_volume) }}</el-descriptions-item>
              <el-descriptions-item label="卖一">{{ formatOrderbookLevel(spot.orderbook?.ask1_price, spot.orderbook?.ask1_volume) }}</el-descriptions-item>
              <el-descriptions-item label="买五总量">{{ formatLot(spot.orderbook?.bid_depth_5) }}</el-descriptions-item>
              <el-descriptions-item label="卖五总量">{{ formatLot(spot.orderbook?.ask_depth_5) }}</el-descriptions-item>
              <el-descriptions-item label="盘口价差">{{ formatSpread(spot.orderbook?.bid_ask_spread) }}</el-descriptions-item>
              <el-descriptions-item label="盘口失衡">{{ orderbookImbalanceDisplay }}</el-descriptions-item>
            </el-descriptions>
            <div class="orderbook-hint">
              <span>{{ supportStrengthHint }}</span>
              <span>{{ sealQualityHint }}</span>
              <span>{{ withdrawalRatioHint }}</span>
            </div>
          </el-card>
        </div>
      </el-tab-pane>

      <!-- Tab 2: K线图(专业版: 主图+副图+指标切换) -->
      <el-tab-pane label="K线" name="kline">
        <div class="kline-toolbar">
          <div class="kline-indicator-group">
            <span class="toolbar-label">数据:</span>
            <el-radio-group v-model="klineView" size="small" @change="reloadKlineView">
              <el-radio-button value="projection">日常采集</el-radio-button>
              <el-radio-button value="downloaded">下载历史</el-radio-button>
            </el-radio-group>
          </div>
          <div class="kline-indicator-group">
            <span class="toolbar-label">主图指标:</span>
            <el-radio-group v-model="mainIndicator" size="small">
              <el-radio-button value="ma">MA均线</el-radio-button>
              <el-radio-button value="boll">BOLL</el-radio-button>
              <el-radio-button value="none">隐藏</el-radio-button>
            </el-radio-group>
          </div>
          <div class="kline-indicator-group">
            <span class="toolbar-label">副图指标:</span>
            <el-radio-group v-model="subIndicator" size="small">
              <el-radio-button value="macd">MACD</el-radio-button>
              <el-radio-button value="rsi">RSI</el-radio-button>
              <el-radio-button value="kdj">KDJ</el-radio-button>
              <el-radio-button value="none">隐藏</el-radio-button>
            </el-radio-group>
          </div>
          <div class="kline-legend" v-if="displayIndicators">
            <span class="is-focus-mode" :class="{ 'is-hovering': isHoveringKline }">
              {{ isHoveringKline ? '跟随悬停K线' : '最新K线' }}
            </span>
            <span v-if="displayIndicators.vol_ma5 != null" class="is-volume">
              VOL5: <b>{{ formatStockVolumeCompact(displayIndicators.vol_ma5) }}</b>
            </span>
            <span v-if="displayIndicators.volume != null" class="is-volume-light">
              当日量: <b>{{ formatStockVolumeCompact(displayIndicators.volume) }}</b>
            </span>
            <span v-if="mainIndicator==='ma' && displayIndicators.ma20">
              MA20: <b :class="changeColorClass(displayIndicators.close - displayIndicators.ma20)">{{ displayIndicators.ma20 }}</b>
            </span>
            <span v-if="mainIndicator==='boll' && displayIndicators.boll_mid">
              BOLL中轨: <b>{{ displayIndicators.boll_mid }}</b> 上轨: {{ displayIndicators.boll_upper }} 下轨: {{ displayIndicators.boll_lower }}
            </span>
            <span v-if="subIndicator==='macd' && displayIndicators.dif!=null">
              DIF: <b :class="changeColorClass(displayIndicators.dif)">{{ displayIndicators.dif }}</b>
              DEA: {{ displayIndicators.dea }} MACD: <b :class="changeColorClass(displayIndicators.macd)">{{ displayIndicators.macd }}</b>
            </span>
            <span v-if="subIndicator==='rsi' && displayIndicators.rsi14!=null">
              RSI6: <b :style="{color: rsiColor(displayIndicators.rsi6)}">{{ displayIndicators.rsi6 }}</b>
              RSI14: <b :style="{color: rsiColor(displayIndicators.rsi14)}">{{ displayIndicators.rsi14 }}</b>
            </span>
            <span v-if="subIndicator==='kdj' && displayIndicators.kdj_k!=null">
              K:<b>{{ displayIndicators.kdj_k }}</b> D:<b>{{ displayIndicators.kdj_d }}</b> J:<b :class="changeColorClass(displayIndicators.kdj_j - 50)">{{ displayIndicators.kdj_j }}</b>
            </span>
          </div>
        </div>
        <el-alert
          v-if="klineView === 'downloaded'"
          data-testid="downloaded-history-note"
          type="warning"
          :closable="false"
          show-icon
          :title="klineMetadata.downloaded_at
            ? '历史下载 · ' + klineMetadata.downloaded_at + ' · 前复权快照，仅供看图，覆盖未认证，不用于历史交易证明'
            : '尚无有效历史下载；请先运行 README 中的新电脑历史下载命令'"
          :description="klineMetadata.coverage?.unavailable_years?.length
            ? '未取到年份：' + klineMetadata.coverage.unavailable_years.join('、') + '（可能上市前或源故障，不能认定完整）'
            : ''"
        />
        <div class="panel-card kline-panel" :class="{ 'is-dragging': klineIsDragging }" tabindex="0" @keydown="handleKlineKeydown">
          <div v-if="displayIndicators" class="kline-side-panel">
            <div class="kline-side-panel-head">
              <div class="kline-side-panel-date">
                <span class="kline-side-panel-mode" :class="{ 'is-hovering': isHoveringKline }">
                  {{ isHoveringKline ? '十字读数' : '最新读数' }}
                </span>
                <span class="kline-side-panel-date-text">{{ displayIndicators.trade_date || '--' }}</span>
              </div>
              <div class="kline-side-panel-price-line">
                <span class="kline-side-panel-price" :class="changeColorClass(displayIndicators.change_pct)">{{ displayIndicators.close ?? '--' }}</span>
                <span class="kline-side-panel-change" :class="changeColorClass(displayIndicators.change_pct)">{{ formatChange(displayIndicators.change_pct) }}</span>
              </div>
            </div>
            <div class="kline-side-panel-meta">
              <span>量 {{ formatStockVolumeCompact(displayIndicators.volume) }}</span>
              <span>VOL5 {{ formatStockVolumeCompact(displayIndicators.vol_ma5) }}</span>
            </div>
            <div class="kline-side-panel-meta">
              <span>额 {{ formatStockAmountCompact(displayIndicators.amount) }}</span>
              <span>换手 {{ displayIndicators.turnover ? `${displayIndicators.turnover}%` : '--' }}</span>
              <span>振幅 {{ displayIndicators.amplitude ? `${displayIndicators.amplitude}%` : '--' }}</span>
            </div>
            <div class="kline-side-panel-divider"></div>
            <div class="kline-side-panel-grid">
              <span class="kline-side-panel-key">开</span>
              <span>{{ displayIndicators.open ?? '--' }}</span>
              <span class="kline-side-panel-key">高</span>
              <span>{{ displayIndicators.high ?? '--' }}</span>
              <span class="kline-side-panel-key">低</span>
              <span>{{ displayIndicators.low ?? '--' }}</span>
              <span class="kline-side-panel-key">昨</span>
              <span>{{ displayIndicators.prev_close ?? '--' }}</span>
            </div>
          </div>
          <v-chart
            v-if="klines.length"
            ref="klineChartRef"
            :option="klineFullOption"
            :init-options="klineChartInitOptions"
            style="height: 600px"
            autoresize
          />
          <div v-else class="kline-loading">
            <span style="font-size:14px">{{ klineLoading ? '加载K线数据...' : (klineError || '暂无K线数据') }}</span>
          </div>
          <div class="kline-shortcuts">滚轮缩放 | 左键拖拽平移 | 双击复位 | 拖动底部滑块平移 | ←→键移动</div>
        </div>
      </el-tab-pane>

      <!-- Tab 3: 资金流 -->
      <el-tab-pane label="资金流" name="fund-flow">
        <div class="fund-flow-note">
          东财主口径：主力 = 超大单 + 大单。概况页中的“即时资金净额(参考)”为盘口侧参考值，不与东财主力资金混用。
        </div>
        <v-chart :option="fundFlowChartOption" style="height: 300px" autoresize />
        <el-table :data="fundFlowList" stripe size="small" empty-text="暂无数据" class="mt-16">
          <el-table-column prop="trade_date" label="日期" width="110" />
          <el-table-column prop="main_net_inflow" label="主力资金净额" width="140" align="right">
            <template #default="{ row }"><span :class="changeColorClass(row.main_net_inflow)">{{ formatAmount(row.main_net_inflow) }}</span></template>
          </el-table-column>
          <el-table-column prop="big_net_inflow" label="东财大单净额" width="120" align="right">
            <template #default="{ row }"><span :class="changeColorClass(row.big_net_inflow)">{{ formatAmount(row.big_net_inflow) }}</span></template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- Tab 4: 信号评分 -->
      <el-tab-pane label="信号评分" name="score">
        <div class="score-section" v-if="score.bull && score.bull.total_score">
          <!-- V2.2融合: 实时行情分析指标条 -->
          <div class="realtime-indicators" v-if="score.bull.volume_ratio_level || score.bull.turnover_level">
            <div class="indicator-item">
              <span class="indicator-label">量比</span>
              <el-tag :type="vrTagType(score.bull.volume_ratio_level)" size="small" effect="light">{{ score.bull.volume_ratio_level || '正常' }}</el-tag>
            </div>
            <div class="indicator-item">
              <span class="indicator-label">换手率</span>
              <el-tag :type="trTagType(score.bull.turnover_level)" size="small" effect="light">{{ score.bull.turnover_level || '正常' }}</el-tag>
            </div>
            <div class="indicator-item">
              <span class="indicator-label">量价关系</span>
              <el-tag :type="pvTagType(score.bull.price_volume_relation)" size="small" effect="light">{{ score.bull.price_volume_relation || '缩量盘整' }}</el-tag>
            </div>
            <div class="indicator-item" v-if="score.bull.chip_signal > 0">
              <span class="indicator-label">筹码信号</span>
              <span class="chip-signal" :class="chipSignalClass(score.bull.chip_signal)">{{ score.bull.chip_signal.toFixed(1) }}/10</span>
            </div>
          </div>

          <!-- V2.2融合: 筹码分析卡片 -->
          <div class="chip-card" v-if="score.chip && score.chip.score > 0">
            <div class="section-title">筹码分析 (V2.2)</div>
            <div class="chip-metrics-grid">
              <div class="chip-metric">
                <span class="chip-metric-label">集中度评分</span>
                <span class="chip-metric-value" :class="chipScoreClass(score.chip.score)">{{ Math.round(score.chip.score) }}</span>
              </div>
              <div class="chip-metric">
                <span class="chip-metric-label">状态</span>
                <el-tag :type="chipStatusType(score.chip.status)" size="small" effect="light">{{ chipStatusLabel(score.chip.status) }}</el-tag>
              </div>
              <div class="chip-metric">
                <span class="chip-metric-label">形态</span>
                <span class="chip-metric-value">{{ score.chip.pattern || '--' }}</span>
              </div>
              <div class="chip-metric">
                <span class="chip-metric-label">信号强度</span>
                <span class="chip-metric-value" :class="chipSignalClass(score.chip.signal_strength)">{{ score.chip.signal_strength.toFixed(1) }}/10</span>
              </div>
              <div class="chip-metric">
                <span class="chip-metric-label">90%集中度</span>
                <span class="chip-metric-value">{{ (score.chip.concentration_90 * 100).toFixed(1) }}%</span>
              </div>
              <div class="chip-metric">
                <span class="chip-metric-label">70%集中度</span>
                <span class="chip-metric-value">{{ (score.chip.concentration_70 * 100).toFixed(1) }}%</span>
              </div>
              <div class="chip-metric">
                <span class="chip-metric-label">获利盘</span>
                <span class="chip-metric-value">{{ (score.chip.profit_ratio * 100).toFixed(1) }}%</span>
              </div>
              <div class="chip-metric">
                <span class="chip-metric-label">平均成本</span>
                <span class="chip-metric-value">{{ score.chip.avg_cost ? score.chip.avg_cost.toFixed(2) : '--' }}</span>
              </div>
            </div>
            <div class="chip-levels" v-if="score.chip.support_levels?.length || score.chip.resistance_levels?.length">
              <div class="chip-level-row" v-if="score.chip.support_levels?.length">
                <span class="level-label">支撑位</span>
                <div class="level-tags">
                  <el-tag v-for="s in score.chip.support_levels" :key="s" type="success" size="small" effect="plain">{{ s }}</el-tag>
                </div>
              </div>
              <div class="chip-level-row" v-if="score.chip.resistance_levels?.length">
                <span class="level-label">压力位</span>
                <div class="level-tags">
                  <el-tag v-for="r in score.chip.resistance_levels" :key="r" type="danger" size="small" effect="plain">{{ r }}</el-tag>
                </div>
              </div>
            </div>
            <div class="chip-desc" v-if="score.chip.description">{{ score.chip.description }}</div>
          </div>

          <!-- V2.2融合: 量化交易信号列表 -->
          <div class="signal-list-card" v-if="score.bull?.top_signals?.length">
            <div class="section-title">核心信号 (量化交易)</div>
            <div class="signal-list">
              <div v-for="(sig, idx) in score.bull.top_signals" :key="idx" class="signal-item">
                <el-tag :type="signalTagType(sig)" size="small" effect="light">{{ signalCategory(sig) }}</el-tag>
                <span class="signal-text">{{ sig }}</span>
              </div>
            </div>
          </div>

          <div class="score-dual">
            <div class="score-card">
              <div class="section-title">短线评分 (BullScore v3.1)</div>
              <v-chart :option="bullRadarOption" style="height: 300px" autoresize />
              <div v-if="score.bull.risk_warnings?.length" class="section-title mt-12">风险预警</div>
              <el-alert v-for="w in score.bull.risk_warnings || []" :key="w" :title="w" type="warning" :closable="false" show-icon style="margin-bottom:8px" />
              <el-alert v-if="score.bull.suggestion" :title="score.bull.suggestion" type="info" :closable="false" show-icon class="mt-12" />
            </div>
            <div class="score-card" v-if="score.tenbagger && score.tenbagger.total_score">
              <div class="section-title">十倍潜力 (Tenbagger)</div>
              <v-chart :option="tenbaggerRadarOption" style="height: 300px" autoresize />
              <div v-if="score.tenbagger.highlights?.length" class="section-title mt-12">亮点</div>
              <el-alert v-for="h in score.tenbagger.highlights || []" :key="h" :title="h" type="success" :closable="false" show-icon style="margin-bottom:8px" />
              <div v-if="score.tenbagger.risks?.length" class="section-title mt-12">风险</div>
              <el-alert v-for="r in score.tenbagger.risks || []" :key="r" :title="r" type="warning" :closable="false" show-icon style="margin-bottom:8px" />
            </div>
          </div>
        </div>
        <el-empty v-else description="暂无评分数据" :image-size="60" />
      </el-tab-pane>

      <!-- Tab 5: 板块共振 -->
      <el-tab-pane label="板块共振" name="resonance">
        <div class="panel-card" v-if="resonance.sectors?.length">
          <div class="section-title">共振评分: <strong :style="{color: resonanceColor(resonance.best_resonance_score)}">{{ resonance.best_resonance_score }}</strong> {{ resonanceLabel(resonance.best_level) }}</div>
          <el-table :data="resonance.sectors" stripe size="small">
            <el-table-column prop="sector_name" label="板块" min-width="120" />
            <el-table-column prop="sector_change" label="板块涨幅" width="100" align="right">
              <template #default="{ row }"><span :class="changeColorClass(row.sector_change)">{{ formatChange(row.sector_change) }}</span></template>
            </el-table-column>
            <el-table-column prop="direction_score" label="方向分" width="80" align="center" />
            <el-table-column prop="fund_score" label="资金分" width="80" align="center" />
            <el-table-column prop="lifecycle_score" label="生命周期分" width="100" align="center" />
            <el-table-column prop="total_score" label="总分" width="80" align="center">
              <template #default="{ row }"><strong :style="{color: resonanceColor(row.total_score)}">{{ row.total_score }}</strong></template>
            </el-table-column>
            <el-table-column prop="level" label="等级" width="100" align="center">
              <template #default="{ row }"><el-tag :type="resonanceTagType(row.level)" size="small">{{ resonanceLabel(row.level) }}</el-tag></template>
            </el-table-column>
          </el-table>
        </div>
        <el-empty v-else description="暂无共振数据" :image-size="60" />
      </el-tab-pane>

      <!-- Tab 6: 概念关联(替代新闻催化) -->
      <el-tab-pane label="概念关联" name="concepts">
        <div class="panel-card">
          <el-table :data="concepts" stripe size="small" empty-text="暂无概念数据">
            <el-table-column prop="sector_name" label="概念" min-width="150" />
            <el-table-column prop="sector_type" label="类型" width="80" align="center">
              <template #default="{ row }"><el-tag size="small">{{ row.sector_type === 'concept' ? '概念' : '行业' }}</el-tag></template>
            </el-table-column>
            <el-table-column prop="source" label="来源" width="80" />
          </el-table>
        </div>
      </el-tab-pane>

      <!-- Tab 7: 操作预案 -->
      <el-tab-pane label="操作预案" name="plan">
        <div class="panel-card" v-if="plan.strategies?.length">
          <div v-if="plan.data_version" class="plan-data-meta">
            <el-tag
              size="small"
              :type="plan.quality_status === 'degraded' ? 'warning' : plan.is_intraday_provisional ? 'info' : 'success'"
              effect="plain"
            >
              {{ plan.quality_status === 'degraded' ? '数据降级' : plan.is_intraday_provisional ? '盘中临时预案' : '完整日线预案' }}
            </el-tag>
            <span>行情截至 {{ formatPlanDateTime(plan.quote_as_of) }}</span>
            <span>技术基准 {{ plan.technical_trade_date || '--' }}</span>
            <span v-if="plan.fund_5d_end_date">资金截至 {{ plan.fund_5d_end_date }}</span>
          </div>
          <el-alert
            v-if="plan.data_version && plan.quality_warnings?.length"
            class="plan-quality-alert"
            :title="plan.quality_warnings.join('；')"
            type="warning"
            :closable="false"
            show-icon
          />
          <el-alert
            v-else-if="!plan.data_version"
            class="plan-quality-alert"
            title="当前仍是旧版预案响应，交易日、完整日线和数据质量状态不可验证"
            type="warning"
            :closable="false"
            show-icon
          />

          <!-- 评分概览 -->
          <div class="plan-header">
            <div class="plan-header-item">
              <span class="plan-label">评级</span>
              <el-tag :color="levelColor(plan.bull_level)" size="small" effect="dark" style="border:none;color:#fff">{{ plan.bull_level }}</el-tag>
            </div>
            <div class="plan-header-item">
              <span class="plan-label">评分</span>
              <span class="plan-val">{{ plan.bull_score }}</span>
            </div>
            <div class="plan-header-item">
              <span class="plan-label">{{ plan.fund_5d_label || '近5个完整交易日' }}资金</span>
              <span :class="plan.fund_5d_direction === '净流入' ? 'text-red' : plan.fund_5d_direction === '净流出' ? 'text-green' : ''">
                {{ plan.fund_5d_billion == null ? '数据不足' : `${plan.fund_5d_direction} ${formatPlanBillion(plan.fund_5d_billion)}` }}
              </span>
            </div>
            <div class="plan-header-item" v-if="plan.current_main_net_inflow_billion != null">
              <span class="plan-label">{{ plan.current_fund_is_partial ? '今日盘中资金' : '当日资金' }}</span>
              <span :class="Number(plan.current_main_net_inflow_billion) > 0 ? 'text-red' : Number(plan.current_main_net_inflow_billion) < 0 ? 'text-green' : ''">
                {{ formatPlanBillion(plan.current_main_net_inflow_billion) }}
              </span>
            </div>
            <div class="plan-header-item">
              <span class="plan-label">技术</span>
              <span class="tech-tag">{{ plan.tech_summary }}</span>
            </div>
            <div class="plan-header-item">
              <span class="plan-label">板块</span>
              <span>{{ plan.sector_status }}</span>
              <el-tag v-if="plan.sector_resonance" size="small" :type="resonanceType(plan.sector_resonance)" effect="plain" style="margin-left:4px">{{ plan.sector_resonance }}</el-tag>
            </div>
            <div class="plan-header-item" v-if="plan.sentiment_cycle">
              <span class="plan-label">情绪</span>
              <span class="sentiment-badge">{{ plan.sentiment_cycle }}</span>
            </div>
            <div class="plan-header-item" v-if="plan.market_environment">
              <span class="plan-label">大盘</span>
              <span :class="plan.market_environment === 'strong' ? 'text-red' : plan.market_environment === 'weak' ? 'text-green' : ''">{{ {strong: '强势', neutral: '中性', weak: '弱势'}[plan.market_environment] }}</span>
            </div>
          </div>

          <!-- 策略卡片 -->
          <div class="plan-strategies">
            <div v-for="s in plan.strategies" :key="s.strategy_type" class="strategy-card" :class="s.strategy_type">
              <div class="strategy-card-header">
                <el-tag :type="strategyType(s.strategy_type)" effect="dark" size="small">{{ s.strategy_label }}</el-tag>
                <span v-if="s.confidence" class="confidence-badge" :class="s.confidence">信心{{ s.confidence }}</span>
              </div>
              <el-descriptions :column="2" size="small" border v-if="s.strategy_type !== 'avoid' && s.strategy_type !== 'watch'">
                <el-descriptions-item label="买入条件">{{ s.entry_condition }}</el-descriptions-item>
                <el-descriptions-item label="入场价位">{{ s.entry_price_hint }}</el-descriptions-item>
                <el-descriptions-item label="仓位">{{ s.position_ratio }}</el-descriptions-item>
                <el-descriptions-item label="风险收益比">
                  <span v-if="s.risk_reward_ratio">R={{ s.risk_reward_ratio }}</span>
                  <span v-else>--</span>
                </el-descriptions-item>
                <el-descriptions-item label="止损">
                  <span class="text-green">{{ s.stop_loss?.toFixed(2) }}</span>
                  <span class="pct-label">( -{{ s.stop_loss_pct }}% )</span>
                </el-descriptions-item>
                <el-descriptions-item label="目标价">
                  <span class="text-red">{{ s.target_price?.toFixed(2) }}</span>
                  <span class="pct-label">( +{{ s.target_price_pct }}% )</span>
                </el-descriptions-item>
              </el-descriptions>
              <div v-else class="avoid-info">
                <span style="color:#ef4444">{{ s.invalidation }}</span>
              </div>
              <div class="strategy-invalidation" v-if="s.strategy_type !== 'avoid'">
                <span class="inv-label">失效条件:</span>
                <span style="color:#ef4444; font-size:12px">{{ s.invalidation }}</span>
              </div>
            </div>
          </div>

          <!-- 支撑/压力位 -->
          <div class="plan-sr-section">
            <div class="section-title mt-12">支撑/压力位</div>
            <el-descriptions :column="2" size="small" border>
              <el-descriptions-item label="第一支撑">{{ formatPlanPrice(plan.support_detail?.primary) }} <span class="sr-src">{{ plan.support_detail?.source }}</span></el-descriptions-item>
              <el-descriptions-item label="第二支撑">{{ formatPlanPrice(plan.support_detail?.secondary) }}</el-descriptions-item>
              <el-descriptions-item label="第一压力">{{ formatPlanPrice(plan.resistance_detail?.primary) }} <span class="sr-src">{{ plan.resistance_detail?.source }}</span></el-descriptions-item>
              <el-descriptions-item label="第二压力">{{ formatPlanPrice(plan.resistance_detail?.secondary) }}</el-descriptions-item>
            </el-descriptions>
          </div>

          <!-- 不买原因 -->
          <div v-if="plan.avoid_reasons?.length" class="plan-avoid-section">
            <div class="section-title mt-12" style="color:#ef4444">⚠️ 风险提示</div>
            <el-alert v-for="r in plan.avoid_reasons" :key="r" :title="r" type="warning" :closable="false" show-icon style="margin-bottom:6px" />
          </div>

          <!-- 信号/风险 -->
          <div v-if="plan.top_signals?.length || plan.risk_warnings?.length" class="plan-signals-section">
            <div class="section-title mt-12">信号与风险</div>
            <div class="signal-list">
              <el-tag v-for="sig in plan.top_signals || []" :key="sig" size="small" type="danger" effect="plain" style="margin:2px">{{ sig }}</el-tag>
              <el-tag v-for="warn in plan.risk_warnings || []" :key="warn" size="small" type="warning" effect="plain" style="margin:2px">{{ warn }}</el-tag>
            </div>
          </div>
        </div>
        <el-empty v-else description="暂无预案数据" :image-size="60" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { defineAsyncComponent, ref, computed, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { useRoute } from 'vue-router'
const VChart = defineAsyncComponent(() => import('vue-echarts'))
import { ensureStockDetailChartsRegistered } from '@/composables/echarts/stock-detail'
import {
  getStockSpot, getStockKline, getStockFundFlow, getTenbaggerScore,
  getStockResonance, getStockNextDayPlan, getStockProfile,
  getStockFactors
} from '@/api'
import { formatChange, changeColorClass, formatAmount, levelColor, stockTagLabel } from '@/composables/useUtils'

ensureStockDetailChartsRegistered()

const route = useRoute()
const code = computed(() => String(route.params.code || ''))

const activeTab = ref('profile')
const spot = ref({})
const spotFundStatusLabel = computed(() => ({
  ok: '本次查询合格', stale: '资金过期', future: '资金时钟异常',
  invalid: '资金时钟异常', invalid_values: '资金数值待核',
  unsupported_source: '来源待核', unknown: '资金未知', missing: '资金未知',
}[spot.value.main_fund_status] || '资金未知'))
const spotFundHint = computed(() => `${spotFundStatusLabel.value}；拒绝原因：${spot.value.main_fund_reason || '未提供'}；仅为本次查询结果，不替代交易时重新校验。`)
const profile = ref({})
const tag = ref('')
const klines = ref([])
const klineLoading = ref(false)
const klineError = ref('')
const klineView = ref('projection')
const klineMetadata = ref({})
let klineRequestId = 0
const fundFlowList = ref([])
const score = ref({})
const resonance = ref({})
const plan = ref({})
const concepts = ref([])

// K线图指标切换
const mainIndicator = ref('ma')  // 'ma' | 'boll' | 'none'
const subIndicator = ref('macd') // 'macd' | 'rsi' | 'kdj' | 'none'
const klineChartRef = ref(null)
const klineGraphicWidth = ref(0)
const klineRightGutter = ref(288)
const hoveredKlineIndex = ref(null)
let klineChartHoverBound = false
let klineChartWheelBound = false
let klineWheelDom = null
let klineWheelHandler = null
let klineDblclickHandler = null
let klineMouseDownHandler = null
let klineMouseMoveHandler = null
let klineMouseUpHandler = null
const klineIsDragging = ref(false)
const klineDragState = {
  active: false,
  startX: 0,
  startStart: 0,
  startEnd: 100,
}
let stockLoadVersion = 0

const getKlineChartInstance = () => klineChartRef.value?.chart || klineChartRef.value || null
const getKlineDefaultZoomStart = () => {
  const len = klines.value.length || 1
  return Math.max(0, 100 - Math.round(90 / len * 100))
}
const clampZoomWindow = (start, end) => {
  let nextStart = start
  let nextEnd = end
  const minRange = 6
  const maxRange = 100
  let range = nextEnd - nextStart
  if (range < minRange) {
    const mid = (nextStart + nextEnd) / 2
    nextStart = mid - minRange / 2
    nextEnd = mid + minRange / 2
    range = minRange
  }
  if (range > maxRange) {
    nextStart = 0
    nextEnd = 100
    range = maxRange
  }
  if (nextStart < 0) {
    nextEnd = Math.min(100, nextEnd - nextStart)
    nextStart = 0
  }
  if (nextEnd > 100) {
    nextStart = Math.max(0, nextStart - (nextEnd - 100))
    nextEnd = 100
  }
  return {
    start: Math.max(0, Number(nextStart.toFixed(2))),
    end: Math.min(100, Number(nextEnd.toFixed(2))),
  }
}
const dispatchKlineZoom = (start, end) => {
  const chart = getKlineChartInstance()
  if (!chart) return
  const next = clampZoomWindow(start, end)
  chart.dispatchAction({ type: 'dataZoom', start: next.start, end: next.end })
}

const klineChartInitOptions = computed(() => ({
  renderer: 'canvas',
  devicePixelRatio:
    typeof window === 'undefined'
      ? 2.25
      : Math.min(Math.max(window.devicePixelRatio || 1, 2), 2.5),
  useDirtyRect: false,
}))

const syncKlineLayoutMetrics = () => {
  if (typeof window === 'undefined') return
  klineRightGutter.value = window.innerWidth <= 768 ? 60 : 228
}

const resetHoveredKline = () => {
  hoveredKlineIndex.value = null
}

const bindKlineHoverEvents = () => {
  const chart = getKlineChartInstance()
  if (!chart || klineChartHoverBound) return

  chart.on('updateAxisPointer', (event) => {
    const axisInfo = event?.axesInfo?.[0]
    const index = axisInfo?.value
    if (Number.isInteger(index) && index >= 0 && index < klines.value.length) {
      hoveredKlineIndex.value = index
      return
    }
    resetHoveredKline()
  })

  chart.on('globalout', () => {
    resetHoveredKline()
  })

  klineChartHoverBound = true

  const dom = typeof chart.getDom === 'function' ? chart.getDom() : null
  if (!dom || klineChartWheelBound) return

  klineWheelDom = dom
  dom.style.cursor = 'grab'
  klineWheelHandler = (event) => {
    if (!klines.value.length) return
    if (Math.abs(event.deltaY) < 1) return

    const opt = chart.getOption?.()
    const dz = opt?.dataZoom?.[0]
    if (!dz) return

    event.preventDefault()

    const currentStart = Number(dz.start ?? getKlineDefaultZoomStart())
    const currentEnd = Number(dz.end ?? 100)
    const currentRange = currentEnd - currentStart
    const rect = dom.getBoundingClientRect()
    const usableLeft = 64
    const usableWidth = Math.max(rect.width - usableLeft - klineRightGutter.value, 80)
    const pointerRatio = Math.min(
      0.98,
      Math.max(0.02, (event.clientX - rect.left - usableLeft) / usableWidth),
    )

    if (event.shiftKey) {
      const panStep = Math.max(1.25, currentRange * 0.12)
      const direction = event.deltaY > 0 ? 1 : -1
      dispatchKlineZoom(currentStart + panStep * direction, currentEnd + panStep * direction)
      return
    }

    const zoomFactor = event.deltaY > 0 ? 1.14 : 0.88
    const nextRange = currentRange * zoomFactor
    const anchor = currentStart + currentRange * pointerRatio
    const nextStart = anchor - nextRange * pointerRatio
    const nextEnd = anchor + nextRange * (1 - pointerRatio)
    dispatchKlineZoom(nextStart, nextEnd)
  }
  klineDblclickHandler = (event) => {
    event.preventDefault()
    dispatchKlineZoom(getKlineDefaultZoomStart(), 100)
  }
  klineMouseDownHandler = (event) => {
    if (event.button !== 0 || !klines.value.length) return
    const opt = chart.getOption?.()
    const dz = opt?.dataZoom?.[0]
    if (!dz) return
    klineDragState.active = true
    klineDragState.startX = event.clientX
    klineDragState.startStart = Number(dz.start ?? getKlineDefaultZoomStart())
    klineDragState.startEnd = Number(dz.end ?? 100)
    klineIsDragging.value = false
    dom.style.cursor = 'grabbing'
  }
  klineMouseMoveHandler = (event) => {
    if (!klineDragState.active) return
    const rect = dom.getBoundingClientRect()
    const usableLeft = 64
    const usableWidth = Math.max(rect.width - usableLeft - klineRightGutter.value, 80)
    const deltaX = event.clientX - klineDragState.startX
    if (Math.abs(deltaX) < 2) return
    event.preventDefault()
    klineIsDragging.value = true
    const range = klineDragState.startEnd - klineDragState.startStart
    const shift = -(deltaX / usableWidth) * range
    dispatchKlineZoom(klineDragState.startStart + shift, klineDragState.startEnd + shift)
  }
  klineMouseUpHandler = () => {
    klineDragState.active = false
    klineIsDragging.value = false
    dom.style.cursor = 'grab'
  }
  dom.addEventListener('wheel', klineWheelHandler, { passive: false })
  dom.addEventListener('dblclick', klineDblclickHandler)
  dom.addEventListener('mousedown', klineMouseDownHandler)
  window.addEventListener('mousemove', klineMouseMoveHandler, { passive: false })
  window.addEventListener('mouseup', klineMouseUpHandler)
  klineChartWheelBound = true
}

const syncKlineGraphicWidth = () => {
  nextTick(() => {
    const chart = getKlineChartInstance()
    const width = typeof chart?.getWidth === 'function' ? chart.getWidth() : 0
    klineGraphicWidth.value = Math.max(width - (64 + klineRightGutter.value), 0)
    bindKlineHoverEvents()
  })
}

const handleWindowResize = () => {
  syncKlineLayoutMetrics()
  syncKlineGraphicWidth()
}

const klineUseSidebarReadout = computed(() => klineRightGutter.value > 100)

// K线图键盘交互: ←→平移, +/-缩放
const handleKlineKeydown = (e) => {
  if (!klineChartRef.value || !klines.value.length) return
  const chart = klineChartRef.value.chart || klineChartRef.value
  if (!chart || typeof chart.getOption !== 'function') return

  const opt = chart.getOption()
  const dz = opt.dataZoom?.[0]
  if (!dz) return

  const step = 2 // 每次平移2%
  const zoomStep = 3 // 每次缩放3%

  let { start, end } = dz
  const range = end - start

  switch (e.key) {
    case 'ArrowLeft':
      e.preventDefault()
      start = Math.max(0, start - step)
      end = start + range
      if (end > 100) { end = 100; start = end - range }
      chart.dispatchAction({ type: 'dataZoom', start, end })
      break
    case 'ArrowRight':
      e.preventDefault()
      end = Math.min(100, end + step)
      start = end - range
      if (start < 0) { start = 0; end = range }
      chart.dispatchAction({ type: 'dataZoom', start, end })
      break
    case '+':
    case '=':
      e.preventDefault()
      if (range > 4) {
        const shrink = zoomStep / 2
        const mid = (start + end) / 2
        start = mid - (range - shrink) / 2
        end = mid + (range - shrink) / 2
        chart.dispatchAction({ type: 'dataZoom', start: Math.max(0, start), end: Math.min(100, end) })
      }
      break
    case '-':
    case '_':
      e.preventDefault()
      if (range < 98) {
        const expand = zoomStep / 2
        const mid = (start + end) / 2
        start = mid - (range + expand) / 2
        end = mid + (range + expand) / 2
        chart.dispatchAction({ type: 'dataZoom', start: Math.max(0, start), end: Math.min(100, end) })
      }
      break
  }
}

// 最新指标值(用于工具栏展示)
const latestIndicators = computed(() => {
  const d = klines.value
  if (!d.length) return null
  return d[d.length - 1] || null
})

const displayIndicators = computed(() => {
  if (Number.isInteger(hoveredKlineIndex.value) && hoveredKlineIndex.value >= 0 && hoveredKlineIndex.value < klines.value.length) {
    return klines.value[hoveredKlineIndex.value] || latestIndicators.value
  }
  return latestIndicators.value
})

const isHoveringKline = computed(() => Number.isInteger(hoveredKlineIndex.value))
const displayStockName = computed(() => {
  const spotName = spot.value?.name
  if (spotName && spotName !== code.value) return spotName
  return profile.value?.name || spotName || code.value
})

// RSI颜色
const rsiColor = (v) => {
  if (v == null) return '#667085'
  if (v > 70) return '#ef4444'
  if (v < 30) return '#22c55e'
  return '#f97316'
}

const hasOrderbook = computed(() => Boolean(spot.value?.orderbook))

const formatLot = (value) => {
  const num = Number(value || 0)
  if (!num) return '--'
  if (num >= 10000) return `${(num / 10000).toFixed(2)}万手`
  return `${Math.round(num)}手`
}

const formatStockVolumeCompact = (value) => {
  const num = Number(value || 0)
  if (!num) return '--'
  if (num >= 1e8) return `${(num / 1e8).toFixed(2)}亿股`
  if (num >= 1e4) return `${(num / 1e4).toFixed(2)}万股`
  return `${Math.round(num)}股`
}

const formatStockAmountCompact = (value) => {
  const num = Number(value || 0)
  if (!num) return '--'
  if (num >= 1e8) return `${(num / 1e8).toFixed(2)}亿`
  if (num >= 1e4) return `${(num / 1e4).toFixed(2)}万`
  return `${Math.round(num)}`
}

const formatSpread = (value) => {
  const num = Number(value || 0)
  return Number.isFinite(num) && num > 0 ? num.toFixed(2) : '--'
}

const formatOrderbookLevel = (price, volume) => {
  const px = Number(price || 0)
  const vol = Number(volume || 0)
  if (!px && !vol) return '--'
  return `${px ? px.toFixed(2) : '--'} / ${formatLot(vol)}`
}

const supportStrengthValue = computed(() => Number(spot.value?.orderbook?.support_strength_score || 0))
const sealQualityValue = computed(() => Number(spot.value?.orderbook?.seal_quality_score || 0))
const withdrawalRatioValue = computed(() => Number(spot.value?.orderbook?.withdrawal_ratio || 0))
const orderbookImbalanceValue = computed(() => Number(spot.value?.orderbook?.orderbook_imbalance || 0))

const supportStrengthDisplay = computed(() => Math.round(supportStrengthValue.value || 0))
const sealQualityDisplay = computed(() => Math.round(sealQualityValue.value || 0))
const withdrawalRatioDisplay = computed(() => {
  if (withdrawalRatioValue.value <= 0) return '0%'
  return `${Math.round(withdrawalRatioValue.value * 100)}%`
})
const orderbookImbalanceDisplay = computed(() => {
  const pct = orderbookImbalanceValue.value * 100
  if (!pct) return '0.0%'
  return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`
})

const supportStrengthType = computed(() => {
  if (supportStrengthValue.value >= 70) return 'danger'
  if (supportStrengthValue.value >= 58) return 'warning'
  if (supportStrengthValue.value > 0) return 'info'
  return ''
})
const sealQualityType = computed(() => {
  if (sealQualityValue.value >= 75) return 'danger'
  if (sealQualityValue.value >= 60) return 'warning'
  if (sealQualityValue.value > 0) return 'info'
  return ''
})
const withdrawalRatioType = computed(() => {
  if (withdrawalRatioValue.value >= 0.35) return 'danger'
  if (withdrawalRatioValue.value >= 0.15) return 'warning'
  return 'success'
})
const imbalanceType = computed(() => {
  if (orderbookImbalanceValue.value >= 0.12) return 'danger'
  if (orderbookImbalanceValue.value <= -0.12) return 'success'
  return 'info'
})

const supportStrengthHint = computed(() => {
  if (supportStrengthValue.value >= 70) return '盘口承接强，买盘相对占优。'
  if (supportStrengthValue.value >= 58) return '盘口承接偏强，可继续观察买盘延续。'
  if (supportStrengthValue.value > 0) return '盘口承接一般，更多依赖后续量价确认。'
  return '暂无有效盘口承接信号。'
})
const sealQualityHint = computed(() => {
  if (sealQualityValue.value >= 75) return '封单质量高，涨停封单更稳。'
  if (sealQualityValue.value >= 60) return '封单质量中等，仍需观察炸板回封。'
  if (sealQualityValue.value > 0) return '封单一般，强度不足以单独确认。'
  return '当前不属于高质量封单状态。'
})
const withdrawalRatioHint = computed(() => {
  if (withdrawalRatioValue.value >= 0.35) return '买盘撤单明显，短线承接存在松动。'
  if (withdrawalRatioValue.value >= 0.15) return '买盘有一定回撤，需留意盘口变化。'
  return '买盘撤单不明显，承接相对稳定。'
})

const tagType = computed(() => {
  const label = stockTagLabel(tag.value)
  if (label === '可交易') return 'success'
  if (label === '仅观察') return 'warning'
  if (label === '不推') return 'danger'
  return 'info'
})

// ===== 专业级K线图(主图+副图+指标叠加) =====
const klineFullOption = computed(() => {
  const data = klines.value
  if (!data.length) return {}

  const dates = data.map(k => k.trade_date?.slice(5) || '')
  const ohlc = data.map(k => [k.open, k.close, k.low, k.high])
  const volumes = data.map(k => k.volume || 0)
  const closes = data.map(k => k.close)
  const opens = data.map(k => k.open)

  const hasSub = subIndicator.value !== 'none'
  const rightGutter = klineRightGutter.value
  const mainTop = 34
  const mainHeight = hasSub ? '51.5%' : '60.2%'
  const volumeTop = hasSub ? '57.6%' : '72.4%'
  const volumeHeight = hasSub ? '18.2%' : '16.8%'
  const subTop = '78.6%'
  const subHeight = '10.8%'
  const zoomBottom = 12

  // ---- grids ----
  const grids = [
    {
      left: 64,
      right: rightGutter,
      top: mainTop,
      height: mainHeight,
      containLabel: false,
      show: true,
      backgroundColor: 'rgba(255,255,255,0.84)',
      borderColor: 'rgba(226,232,240,0.45)',
      borderWidth: 0,
    }, // grid[0] 主图
    {
      left: 64,
      right: rightGutter,
      top: volumeTop,
      height: volumeHeight,
      containLabel: false,
      show: true,
      backgroundColor: 'rgba(243, 246, 252, 0.98)',
      borderColor: 'rgba(148,163,184,0.12)',
      borderWidth: 0,
    }, // grid[1] 量柱
  ]
  // ---- xAxes ----
  const xAxes = [
    { type: 'category', data: dates, gridIndex: 0,
      axisLabel: { show: false }, axisTick: { show: false }, splitLine: { show: false } },
    {
      type: 'category',
      data: dates,
      gridIndex: 1,
      axisLabel: { show: !hasSub, color: '#667085', fontSize: 10, margin: 10 },
      axisTick: { show: false },
      axisLine: { show: false },
      splitLine: { show: false },
    },
  ]
  // ---- yAxes ----
  const yAxes = [
    { gridIndex: 0, position: 'left', scale: true,
      axisLabel: { color: '#667085', fontSize: 10 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: 'rgba(148,163,184,0.035)', type: 'dashed' } } },  // yAxis[0] 价格
    {
      gridIndex: 1,
      position: 'left',
      scale: true,
      splitNumber: 3,
      axisLabel: { show: false },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
    },                                                                                   // yAxis[1] 成交量
  ]

  // ---- series数组 ----
  const series = []
  const legendData = ['K线', '成交量']
  const zoomXIndices = [0, 1]

  // [0] 蜡烛图
  series.push({
    name: 'K线', type: 'candlestick', data: ohlc,
    xAxisIndex: 0, yAxisIndex: 0,
    barMaxWidth: 11,
    itemStyle: { color: '#ef4444', color0: '#22c55e', borderColor: '#ef4444', borderColor0: '#22c55e', borderWidth: 1 },
  })

  // [1] 成交量柱(涨红跌绿)
  const volColors = volumes.map((v, i) =>
    closes[i] >= opens[i] ? 'rgba(239,68,68,0.78)' : 'rgba(34,197,94,0.78)'
  )
  series.push({
    name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1,
    data: volumes.map((v, i) => ({ value: v, itemStyle: { color: volColors[i] } })),
    barWidth: '72%',
    barMaxWidth: 18,
    barMinHeight: 4,
    emphasis: { disabled: true },
    z: 1,
  })

  // 量MA5
  if (data.some(k => k.vol_ma5 != null)) {
    series.push({
      name: '量MA5', type: 'line', xAxisIndex: 1, yAxisIndex: 1,
      data: data.map(k => k.vol_ma5),
      symbol: 'none', lineStyle: { width: 2.6, color: '#f59e0b', opacity: 1 }, smooth: true,
      z: 3,
    })
  }

  // === 主图指标叠加 ===
  if (mainIndicator.value === 'ma') {
    const maCfg = [
      { key: 'ma5', name: 'MA5', color: '#eab308', w: 1 },
      { key: 'ma10', name: 'MA10', color: '#a855f7', w: 1 },
      { key: 'ma20', name: 'MA20', color: '#3b82f6', w: 1.2 },
      { key: 'ma60', name: 'MA60', color: '#22c55e', w: 1 },
    ]
    maCfg.forEach(c => {
      if (data.some(k => k[c.key] != null)) {
        series.push({ name: c.name, type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: data.map(k => k[c.key]), symbol: 'none', lineStyle: { width: c.w, color: c.color }, smooth: true })
      }
    })
    legendData.push('MA5', 'MA10', 'MA20', 'MA60')
  } else if (mainIndicator.value === 'boll') {
    if (data.some(k => k.boll_upper != null)) {
      series.push({ name: 'BOLL上轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: data.map(k => k.boll_upper), symbol: 'none', lineStyle: { width: 1, color: '#f97316' }, smooth: true })
    }
    if (data.some(k => k.boll_mid != null)) {
      series.push({ name: 'BOLL中轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: data.map(k => k.boll_mid), symbol: 'none', lineStyle: { width: 1.2, color: '#3b82f6' }, smooth: true })
    }
    if (data.some(k => k.boll_lower != null)) {
      series.push({ name: 'BOLL下轨', type: 'line', xAxisIndex: 0, yAxisIndex: 0, data: data.map(k => k.boll_lower), symbol: 'none', lineStyle: { width: 1, color: '#f97316' }, smooth: true })
    }
    // BOLL填充: 用上轨的areaStyle
    if (data.some(k => k.boll_upper != null && k.boll_lower != null)) {
      series.push({
        name: 'BOLL区域', type: 'line', xAxisIndex: 0, yAxisIndex: 0,
        data: data.map(k => k.boll_upper),
        symbol: 'none', lineStyle: { width: 0 },
        areaStyle: {
          color: 'rgba(59,130,246,0.06)',
          origin: 'auto',
        },
        z: 1,
        silent: true,
        tooltip: { show: false },
      })
    }
    legendData.push('BOLL上轨', 'BOLL中轨', 'BOLL下轨')
  }

  // === 副图指标 ===
  if (hasSub) {
    // 副图grid/xAxis/yAxis
    grids.push({
      left: 64,
      right: rightGutter,
        top: subTop,
        height: subHeight,
        containLabel: false,
        show: true,
        backgroundColor: 'rgba(247,249,252,0.90)',
        borderColor: 'rgba(148,163,184,0.10)',
        borderWidth: 0,
      }) // grid[2]
    xAxes.push({
      type: 'category', data: dates, gridIndex: 2,
      axisLabel: { color: '#667085', fontSize: 10, margin: 10 },
      axisTick: { show: false }, splitLine: { show: false }, axisLine: { show: false },
    })
    yAxes.push({
      gridIndex: 2, position: 'left',
      axisLabel: { color: '#667085', fontSize: 10 },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { lineStyle: { color: 'rgba(148,163,184,0.07)', type: 'dashed' } },
    })
    zoomXIndices.push(2)

    const subXIdx = 2
    const subYIdx = 2

    if (subIndicator.value === 'macd') {
      if (data.some(k => k.dif != null)) {
        series.push({ name: 'DIF', type: 'line', xAxisIndex: subXIdx, yAxisIndex: subYIdx, data: data.map(k => k.dif), symbol: 'none', lineStyle: { width: 1.5, color: '#3b82f6' } })
      }
      if (data.some(k => k.dea != null)) {
        series.push({ name: 'DEA', type: 'line', xAxisIndex: subXIdx, yAxisIndex: subYIdx, data: data.map(k => k.dea), symbol: 'none', lineStyle: { width: 1.5, color: '#f97316' } })
      }
      if (data.some(k => k.macd != null)) {
        series.push({
          name: 'MACD柱', type: 'bar', xAxisIndex: subXIdx, yAxisIndex: subYIdx,
          data: data.map(k => k.macd), barMaxWidth: 5,
          itemStyle: {
            color: function(params) {
              const v = params.value
              return v >= 0 ? 'rgba(239,68,68,0.7)' : 'rgba(34,197,94,0.7)'
            }
          },
        })
      }
      legendData.push('DIF', 'DEA', 'MACD柱')

    } else if (subIndicator.value === 'rsi') {
      if (data.some(k => k.rsi6 != null)) {
        series.push({ name: 'RSI6', type: 'line', xAxisIndex: subXIdx, yAxisIndex: subYIdx, data: data.map(k => k.rsi6), symbol: 'none', lineStyle: { width: 1.5, color: '#ef4444' } })
      }
      if (data.some(k => k.rsi14 != null)) {
        series.push({ name: 'RSI14', type: 'line', xAxisIndex: subXIdx, yAxisIndex: subYIdx, data: data.map(k => k.rsi14), symbol: 'none', lineStyle: { width: 1.5, color: '#3b82f6' } })
      }
      // 超买70/超卖30参考线 (markLine on first RSI series)
      const rsi6Series = series[series.length - 2] || series[series.length - 1]
      if (rsi6Series) {
        rsi6Series.markLine = {
          silent: true, symbol: 'none',
          data: [
            { yAxis: 70, label: { formatter: '70', color: '#ef4444', fontSize: 9, position: 'insideEndTop' }, lineStyle: { color: 'rgba(239,68,68,0.35)', type: 'dashed', width: 1 } },
            { yAxis: 30, label: { formatter: '30', color: '#22c55e', fontSize: 9, position: 'insideEndTop' }, lineStyle: { color: 'rgba(34,197,94,0.35)', type: 'dashed', width: 1 } },
          ],
        }
      }
      yAxes[yAxes.length - 1].min = 0
      yAxes[yAxes.length - 1].max = 100
      legendData.push('RSI6', 'RSI14')

    } else if (subIndicator.value === 'kdj') {
      if (data.some(k => k.kdj_k != null)) {
        series.push({ name: 'K', type: 'line', xAxisIndex: subXIdx, yAxisIndex: subYIdx, data: data.map(k => k.kdj_k), symbol: 'none', lineStyle: { width: 1.5, color: '#f97316' } })
      }
      if (data.some(k => k.kdj_d != null)) {
        series.push({ name: 'D', type: 'line', xAxisIndex: subXIdx, yAxisIndex: subYIdx, data: data.map(k => k.kdj_d), symbol: 'none', lineStyle: { width: 1.5, color: '#3b82f6' } })
      }
      if (data.some(k => k.kdj_j != null)) {
        series.push({ name: 'J', type: 'line', xAxisIndex: subXIdx, yAxisIndex: subYIdx, data: data.map(k => k.kdj_j), symbol: 'none', lineStyle: { width: 1.5, color: '#a855f7' } })
      }
      const kSeries = series[series.length - 3] || series[series.length - 1]
      if (kSeries) {
        kSeries.markLine = {
          silent: true, symbol: 'none',
          data: [
            { yAxis: 80, label: { formatter: '80', fontSize: 9, position: 'insideEndTop' }, lineStyle: { color: 'rgba(239,68,68,0.35)', type: 'dashed', width: 1 } },
            { yAxis: 20, label: { formatter: '20', fontSize: 9, position: 'insideEndTop' }, lineStyle: { color: 'rgba(34,197,94,0.35)', type: 'dashed', width: 1 } },
          ],
        }
      }
      yAxes[yAxes.length - 1].min = 0
      yAxes[yAxes.length - 1].max = 100
      legendData.push('K', 'D', 'J')
    }
  }

  // 计算DataZoom默认范围(显示最近120根)
  const zoomStart = Math.max(0, 100 - Math.round(120 / data.length * 100))

  return {
    backgroundColor: 'transparent',
    animation: false,
    animationDurationUpdate: 120,
    tooltip: {
      show: true,
      trigger: 'axis',
      showContent: false,
      triggerOn: 'mousemove|click',
      transitionDuration: 0,
      axisPointer: {
        type: 'cross',
        snap: true,
      crossStyle: { color: 'rgba(100,116,139,0.52)', width: 1 },
        label: {
          show: true,
          backgroundColor: 'rgba(51,65,85,0.78)',
          color: '#f8fafc',
          borderRadius: 6,
          padding: [4, 8],
          fontSize: 11,
        },
      },
      confine: true,
    },
    legend: {
      data: legendData,
      textStyle: { color: '#667085', fontSize: 11 },
      top: 4, left: 44, itemWidth: 16, itemHeight: 8, itemGap: 14,
      selectedMode: true,
    },
    graphic: hasSub ? [
      {
        type: 'line',
        left: 64,
        right: rightGutter,
        top: volumeTop,
        silent: true,
        shape: { x1: 0, y1: 0, x2: klineGraphicWidth.value, y2: 0 },
        style: { stroke: 'rgba(148,163,184,0.16)', lineWidth: 1 },
      },
      {
        type: 'line',
        left: 64,
        right: rightGutter,
        top: subTop,
        silent: true,
        shape: { x1: 0, y1: 0, x2: klineGraphicWidth.value, y2: 0 },
        style: { stroke: 'rgba(148,163,184,0.14)', lineWidth: 1 },
      },
    ] : [
      {
        type: 'line',
        left: 64,
        right: rightGutter,
        top: volumeTop,
        silent: true,
        shape: { x1: 0, y1: 0, x2: klineGraphicWidth.value, y2: 0 },
        style: { stroke: 'rgba(148,163,184,0.16)', lineWidth: 1 },
      },
    ],
    axisPointer: { link: [{ xAxisIndex: zoomXIndices }] },
    grid: grids,
    xAxis: xAxes,
    yAxis: yAxes,
    dataZoom: [
      {
        type: 'inside',
        xAxisIndex: zoomXIndices,
        start: zoomStart,
        end: 100,
        zoomOnMouseWheel: false,
        moveOnMouseMove: false,
        moveOnMouseWheel: false,
        preventDefaultMouseMove: false,
        zoomLock: false,
        throttle: 24,
      },
      {
        type: 'slider',
        xAxisIndex: zoomXIndices,
        start: zoomStart,
        end: 100,
        height: 16,
        bottom: zoomBottom,
        borderColor: 'transparent',
        fillerColor: 'rgba(59,130,246,0.20)',
        handleStyle: { color: '#3b82f6', borderColor: '#3b82f6' },
        moveHandleStyle: { color: '#3b82f6' },
        textStyle: { color: '#667085', fontSize: 10 },
        dataBackground: {
          lineStyle: { color: 'rgba(100,116,139,0.42)' },
          areaStyle: { color: 'rgba(148,163,184,0.14)' },
        },
        selectedDataBackground: {
          lineStyle: { color: '#3b82f6' },
          areaStyle: { color: 'rgba(59,130,246,0.20)' },
        },
        moveHandleSize: 5,
        brushSelect: false,
        showDetail: false,
      },
    ],
    series,
  }
})

// 资金流图
const fundFlowChartOption = computed(() => {
  const items = fundFlowList.value.slice(0, 20)
  if (!items.length) return {}
  return {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: ['东财主力', '东财大单', '中单', '小单'], textStyle: { color: '#667085' } },
    grid: { left: 60, right: 20, top: 40, bottom: 30 },
    xAxis: { type: 'category', data: items.map(i => i.trade_date?.slice(5) || ''), axisLabel: { color: '#667085' } },
    yAxis: { type: 'value', axisLabel: { color: '#667085' }, splitLine: { lineStyle: { color: '#f0f2f5' } } },
    series: [
      { name: '主力资金', type: 'bar', stack: 'flow', data: items.map(i => i.main_net_inflow), itemStyle: { color: '#ef4444' } },
      { name: '东财大单', type: 'bar', stack: 'flow', data: items.map(i => i.big_net_inflow), itemStyle: { color: '#f97316' } },
      { name: '中单', type: 'bar', stack: 'flow', data: items.map(i => i.mid_net_inflow), itemStyle: { color: '#3b82f6' } },
      { name: '小单', type: 'bar', stack: 'flow', data: items.map(i => i.small_net_inflow), itemStyle: { color: '#22c55e' } },
    ],
  }
})

// 雷达图
const bullRadarOption = computed(() => {
  const dims = score.value.bull?.dimensions || {}
  const names = Object.keys(dims)
  if (!names.length) return {}
  const labels = { momentum: '动量强度', capital: '资金面', technical: '技术形态', valuation: '估值安全', fundamental: '基本面', scale: '规模适配', activity: '活跃度' }
  return {
    backgroundColor: 'transparent',
    radar: { indicator: names.map(n => ({ name: labels[n] || n, max: 100 })), axisName: { color: '#667085' } },
    series: [{ type: 'radar', data: [{ value: names.map(n => dims[n] || 0), areaStyle: { color: 'rgba(6,182,212,0.3)' }, lineStyle: { color: '#06b6d4' } }] }],
  }
})

const tenbaggerRadarOption = computed(() => {
  const dims = score.value.tenbagger?.dimensions || {}
  const names = Object.keys(dims)
  if (!names.length) return {}
  const labels = { market_cap: '市值评分', growth: '增速潜力', valuation: '估值安全', sector: '赛道共振', capital: '资金面' }
  return {
    backgroundColor: 'transparent',
    radar: { indicator: names.map(n => ({ name: labels[n] || n, max: 100 })), axisName: { color: '#667085' } },
    series: [{ type: 'radar', data: [{ value: names.map(n => dims[n] || 0), areaStyle: { color: 'rgba(239,68,68,0.3)' }, lineStyle: { color: '#ef4444' } }] }],
  }
})

// 共振
const formatPlanPrice = (value) => {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number.toFixed(2) : '--'
}
const formatPlanBillion = (value) => {
  const number = Number(value)
  if (!Number.isFinite(number)) return '--'
  return `${number > 0 ? '+' : ''}${number.toFixed(1)}亿`
}
const formatPlanDateTime = (value) => {
  if (!value) return '--'
  return String(value).replace('T', ' ').slice(0, 19)
}
const resonanceColor = (s) => s >= 80 ? '#ef4444' : s >= 60 ? '#f97316' : s >= 40 ? '#409eff' : '#98a2b3'
const resonanceLabel = (l) => ({ strong_resonance: '强共振', weak_resonance: '弱共振', independent: '独立行情', counter_trend: '逆势' }[l] || l)
const resonanceTagType = (l) => ({ strong_resonance: 'danger', weak_resonance: '弱共振', independent: '', counter_trend: 'info' }[l] || '')
const strategyLabel = (s) => ({ trend: '趋势买法', aggressive: '激进买法', avoid: '不建议', watch: '观望', auction_follow: '竞价跟', cancel: '取消', normal: '正常' }[s] || s)
const strategyType = (s) => ({ trend: '', aggressive: 'danger', avoid: 'info', watch: 'warning', auction_follow: 'danger', cancel: 'info', normal: '' }[s] || '')
const resonanceType = (r) => ({
  '强共振': 'danger',
  '弱共振': 'warning',
  '独立行情': '',
  '逆势': 'info',
  '无共振': 'info',
  '同向走弱': 'warning',
  '个股弱于板块': 'warning',
  '数据不足': 'info',
}[r] || '')

// V2.2融合: 量比/换手/量价/筹码标签颜色
const vrTagType = (level) => ({ '巨量': 'danger', '放量': 'warning', '正常': '', '缩量': 'info', '极度缩量': 'info' }[level] || '')
const trTagType = (level) => ({ '高换手': 'danger', '活跃': 'warning', '正常': '', '低换手': 'info', '死寂': 'info' }[level] || '')
const pvTagType = (relation) => ({ '放量上涨': 'success', '缩量上涨': 'warning', '缩量盘整': '', '缩量下跌': 'info', '放量下跌': 'danger' }[relation] || '')
const chipSignalClass = (val) => val >= 7 ? 'chip-strong' : val >= 5 ? 'chip-normal' : 'chip-weak'

// V2.2融合: 筹码分析辅助函数
const chipScoreClass = (s) => s >= 75 ? 'chip-strong' : s >= 55 ? 'chip-normal' : 'chip-weak'
const chipStatusLabel = (s) => ({ locking: '锁定', accumulating: '收集中', distributing: '派发中', scattering: '散乱' }[s] || s || '--')
const chipStatusType = (s) => ({ locking: 'danger', accumulating: 'warning', distributing: 'info', scattering: 'info' }[s] || '')

// V2.2融合: 量化交易信号分类
const signalCategory = (sig) => {
  if (!sig) return '其他'
  if (/涨停|连板/.test(sig)) return '动量'
  if (/主力|资金|占比/.test(sig)) return '资金'
  if (/均线|BOLL|布林|MACD|RSI|KDJ|突破.*新高/.test(sig)) return '技术'
  if (/放量|缩量|量比|换手/.test(sig)) return '量价'
  if (/涨幅|5日/.test(sig)) return '动量'
  return '综合'
}
const signalTagType = (sig) => {
  const cat = signalCategory(sig)
  return { '动量': 'danger', '资金': 'warning', '技术': '', '量价': 'success', '综合': 'info' }[cat] || 'info'
}

const resetStockState = () => {
  stockLoadVersion += 1
  spot.value = {}
  profile.value = {}
  tag.value = ''
  klines.value = []
  klineMetadata.value = {}
  klineRequestId += 1
  klineLoading.value = false
  klineError.value = ''
  fundFlowList.value = []
  score.value = {}
  resonance.value = {}
  plan.value = {}
  concepts.value = []
  klineLoaded = false
  resetHoveredKline()
}

const loadStockBaseData = async (stockCode, version = stockLoadVersion) => {
  if (!stockCode) return
  const isCurrent = () => code.value === stockCode && version === stockLoadVersion
  await Promise.allSettled([
    getStockProfile(stockCode).then((res) => {
      if (!isCurrent()) return
      profile.value = res?.profile || {}
      tag.value = profile.value?.tag || profile.value?.board_tag || ''
      concepts.value = profile.value?.sectors || []
    }),
    getStockKline(stockCode, { limit: 1 }).then((res) => {
      if (!isCurrent() || spot.value?.price) return
      const latest = res?.klines?.[res.klines.length - 1]
      if (!latest) return
      spot.value = {
        name: profile.value?.name || stockCode,
        price: latest.close,
        prev_close: latest.prev_close,
        open: latest.open,
        high: latest.high,
        low: latest.low,
        change_pct: latest.change_pct,
        change_amt: Number(latest.close || 0) - Number(latest.prev_close || 0),
        amplitude: latest.prev_close
          ? Number((((latest.high || 0) - (latest.low || 0)) / latest.prev_close * 100).toFixed(2))
          : null,
        volume: latest.volume ? latest.volume / 100 : null,
        amount: latest.amount,
        turnover: latest.turnover,
        avg_price: latest.close,
        updated_at: latest.trade_date,
        source: 'kline_fallback',
      }
    }),
    getStockSpot(stockCode).then((res) => {
      if (isCurrent() && !res?.error) spot.value = res || {}
    }),
    getStockFundFlow(stockCode).then((res) => {
      if (isCurrent()) fundFlowList.value = res?.fund_flow || []
    }),
    getTenbaggerScore(stockCode).then((res) => {
      if (isCurrent()) score.value = res || {}
    }),
    getStockResonance(stockCode).then((res) => {
      if (isCurrent()) resonance.value = res || {}
    }),
    getStockNextDayPlan(stockCode).then((res) => {
      if (isCurrent()) plan.value = res || {}
    }),
  ])
}

const loadKlineData = async (stockCode, version = stockLoadVersion) => {
  if (!stockCode) return
  const requestId = ++klineRequestId
  const view = klineView.value
  const isCurrent = () => code.value === stockCode && version === stockLoadVersion
    && requestId === klineRequestId && view === klineView.value
  klineLoaded = true
  klineLoading.value = true
  klineError.value = ''
  klineMetadata.value = {}
  klines.value = []
  try {
    const res = await getStockKline(stockCode, { limit: 800, view })
    if (!isCurrent()) return
    klines.value = res?.klines || []
    klineMetadata.value = res || {}
    resetHoveredKline()
    syncKlineGraphicWidth()
  } catch {
    if (isCurrent()) {
      klineLoaded = false
      klines.value = []
      klineError.value = 'K线数据加载失败'
    }
  } finally {
    if (isCurrent()) klineLoading.value = false
  }
}

const reloadKlineView = () => {
  klineLoaded = false
  loadKlineData(code.value, stockLoadVersion)
}

onMounted(() => {
  syncKlineLayoutMetrics()
  window.addEventListener('resize', handleWindowResize, { passive: true })
})

onUnmounted(() => {
  window.removeEventListener('resize', handleWindowResize)
  if (klineWheelDom && klineWheelHandler) {
    klineWheelDom.removeEventListener('wheel', klineWheelHandler)
  }
  if (klineWheelDom && klineDblclickHandler) {
    klineWheelDom.removeEventListener('dblclick', klineDblclickHandler)
  }
  if (klineWheelDom && klineMouseDownHandler) {
    klineWheelDom.removeEventListener('mousedown', klineMouseDownHandler)
  }
  if (klineMouseMoveHandler) {
    window.removeEventListener('mousemove', klineMouseMoveHandler)
  }
  if (klineMouseUpHandler) {
    window.removeEventListener('mouseup', klineMouseUpHandler)
  }
})

// K线懒加载: 首次切换到K线Tab时才请求数据(解决ECharts在隐藏Tab中初始化宽度为0的问题)
let klineLoaded = false
watch(activeTab, async (tab) => {
  if (tab === 'kline' && !klineLoaded) {
    await loadKlineData(code.value, stockLoadVersion)
  } else if (tab === 'kline') {
    resetHoveredKline()
    syncKlineGraphicWidth()
  } else {
    resetHoveredKline()
  }
})

watch(
  code,
  async (nextCode, prevCode) => {
    if (!nextCode || nextCode === prevCode) return
    resetStockState()
    const version = stockLoadVersion
    loadStockBaseData(nextCode, version)
    if (activeTab.value === 'kline') {
      await loadKlineData(nextCode, version)
    }
  },
  { immediate: true },
)

watch([subIndicator, mainIndicator, klines], () => {
  resetHoveredKline()
  if (activeTab.value === 'kline' && klines.value.length) {
    syncKlineGraphicWidth()
  }
}, { deep: false })
</script>

<style scoped lang="scss">
.stock-detail-page {
  max-width: 1600px;
  margin: 0 auto;
}

.stock-hero {
  position: relative;
  overflow: hidden;
  padding: 18px 22px 20px;
  background:
    radial-gradient(circle at 92% 0%, rgba(14, 165, 233, 0.13), transparent 32%),
    linear-gradient(145deg, #ffffff 0%, #fbfdff 72%, #f4f9ff 100%);
  border: 1px solid var(--claw-border);
  border-radius: 16px;
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.055);

  &::after {
    position: absolute;
    right: -42px;
    bottom: -72px;
    width: 190px;
    height: 190px;
    content: '';
    background: rgba(14, 165, 233, 0.045);
    border-radius: 50%;
    pointer-events: none;
  }

  > * {
    position: relative;
    z-index: 1;
  }
}

.detail-context {
  display: flex;
  align-items: center;
  gap: 10px;
  min-height: 24px;
  margin-bottom: 14px;
  color: var(--claw-text-muted);
  font-size: 12px;
}

.back-link {
  color: var(--claw-primary);
  font-weight: 600;
  text-decoration: none;
  transition: color var(--transition-fast);

  &:hover {
    color: var(--primary-700);
  }
}

.context-divider {
  width: 1px;
  height: 12px;
  background: var(--claw-border);
}

.quote-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-left: auto;

  &::before {
    width: 6px;
    height: 6px;
    content: '';
    background: var(--success-500);
    border-radius: 50%;
    box-shadow: 0 0 0 3px var(--success-100);
  }
}

.stock-header {
  display: grid;
  grid-template-columns: minmax(300px, 1.15fr) minmax(280px, 0.8fr) auto;
  align-items: stretch;
  gap: 20px;
  margin-bottom: 18px;
}

.stock-info {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
}

.stock-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;

  h2 {
    margin: 0;
    color: var(--claw-text-primary);
    font-size: 24px;
    font-weight: 750;
    letter-spacing: -0.025em;
    line-height: 1.25;
  }
}

.stock-code {
  padding: 3px 8px;
  color: var(--claw-text-secondary);
  background: var(--neutral-100);
  border: 1px solid var(--claw-border-light);
  border-radius: 6px;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  letter-spacing: 0.04em;
}

.price-block {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-top: 13px;
  font-variant-numeric: tabular-nums;
}

.current-price {
  font-size: 34px;
  font-weight: 760;
  letter-spacing: -0.04em;
  line-height: 1;
}

.price-change {
  padding: 4px 8px;
  background: var(--neutral-50);
  border: 1px solid var(--claw-border-light);
  border-radius: 7px;
  font-size: 14px;
  font-weight: 700;
}

.price-caption {
  color: var(--claw-text-muted);
  font-size: 12px;
}

.price-placeholder {
  margin-top: 16px;
  color: var(--claw-text-muted);
  font-size: 13px;
}

.market-snapshot {
  display: grid;
  grid-template-columns: repeat(2, minmax(100px, 1fr));
  align-content: center;
  gap: 10px 18px;
  padding: 10px 20px;
  border-right: 1px solid var(--claw-border-light);
  border-left: 1px solid var(--claw-border-light);
}

.snapshot-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;

  span {
    color: var(--claw-text-muted);
    font-size: 12px;
  }

  strong {
    color: var(--claw-text-primary);
    font-size: 14px;
    font-variant-numeric: tabular-nums;
  }
}

.score-badges {
  display: flex;
  align-items: stretch;
  gap: 10px;
}

.badge {
  width: 122px;
  padding: 12px 13px;
  text-align: left;
  background: rgba(255, 255, 255, 0.86);
  border: 1px solid var(--claw-border-light);
  border-radius: 12px;
  box-shadow: 0 4px 14px rgba(15, 23, 42, 0.04);
}

.badge-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 6px;
  color: var(--claw-text-secondary);
  font-size: 11px;
  white-space: nowrap;
}

.score-level {
  color: #fff !important;
  border: none !important;
}

.badge-val {
  margin-top: 8px;
  font-size: 27px;
  font-variant-numeric: tabular-nums;
  font-weight: 760;
  letter-spacing: -0.03em;
  line-height: 1;
}

.badge-foot {
  margin-top: 6px;
  color: var(--claw-text-muted);
  font-size: 10px;
}

.hero-metrics {
  display: grid;
  grid-template-columns: repeat(6, minmax(0, 1fr));
  overflow: hidden;
  background: rgba(248, 250, 252, 0.82);
  border: 1px solid var(--claw-border-light);
  border-radius: 11px;
}

.hero-metric {
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 11px 14px;

  & + & {
    border-left: 1px solid var(--claw-border-light);
  }

  span {
    color: var(--claw-text-muted);
    font-size: 11px;
  }

  strong {
    overflow: hidden;
    color: var(--claw-text-primary);
    font-size: 14px;
    font-variant-numeric: tabular-nums;
    font-weight: 650;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.detail-tabs {
  margin-top: 16px;

  :deep(.el-tabs__header) {
    position: sticky;
    top: 0;
    z-index: 12;
    margin: 0 0 16px;
    padding: 0 10px;
    background: rgba(255, 255, 255, 0.96);
    border: 1px solid var(--claw-border);
    border-radius: 13px;
    box-shadow: 0 5px 16px rgba(15, 23, 42, 0.045);
    backdrop-filter: blur(12px);
  }

  :deep(.el-tabs__nav-wrap::after) {
    display: none;
  }

  :deep(.el-tabs__item) {
    height: 52px;
    padding: 0 20px;
    color: var(--claw-text-secondary);
    font-size: 14px;
    font-weight: 600;
    transition: color var(--transition-fast);
  }

  :deep(.el-tabs__item:hover),
  :deep(.el-tabs__item.is-active) {
    color: var(--claw-primary);
  }

  :deep(.el-tabs__active-bar) {
    height: 3px;
    background: linear-gradient(90deg, var(--primary-400), var(--primary-600));
    border-radius: 999px;
  }

  :deep(.el-tabs__content) {
    overflow: visible;
  }
}

.card-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: 18px;
  align-items: stretch;

  :deep(.el-card) {
    overflow: hidden;
    border-color: var(--claw-border);
    border-radius: 14px;
    box-shadow: 0 6px 18px rgba(15, 23, 42, 0.045);
  }

  :deep(.el-card__body) {
    height: 100%;
    padding: 20px;
  }
}

.panel-card,
.score-card,
.chip-card,
.signal-list-card {
  background: var(--claw-bg-card);
  border: 1px solid var(--claw-border);
  border-radius: 14px;
  box-shadow: 0 6px 18px rgba(15, 23, 42, 0.045);
}

.score-dual {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.score-card {
  padding: 18px;
}

.section-title {
  margin-bottom: 14px;

  &::before {
    width: 3px;
    height: 15px;
    content: '';
    background: linear-gradient(180deg, var(--primary-400), var(--primary-600));
    border-radius: 999px;
  }
}

.section-note {
  margin-left: 4px;
  color: #8a97ad;
  font-size: 12px;
  font-weight: 500;
}

:deep(.el-descriptions__body .el-descriptions__table) {
  overflow: hidden;
  border-radius: 10px;
}

:deep(.el-descriptions__cell) {
  padding: 9px 11px !important;
}

:deep(.el-descriptions__label.el-descriptions__cell.is-bordered-label) {
  color: var(--claw-text-secondary);
  background: var(--neutral-50);
  font-weight: 600;
}

:deep(.el-descriptions__content.el-descriptions__cell.is-bordered-content) {
  color: var(--claw-text-primary);
  font-variant-numeric: tabular-nums;
}

// V2.2融合: 实时行情指标条
.realtime-indicators {
  display: flex;
  gap: 16px;
  padding: 12px 16px;
  background: var(--claw-card-bg);
  border-radius: 10px;
  margin-bottom: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.indicator-item {
  display: flex;
  align-items: center;
  gap: 6px;
}
.indicator-label {
  font-size: 12px;
  color: var(--claw-text-muted);
  white-space: nowrap;
}
.chip-signal {
  font-size: 14px;
  font-weight: 700;
  &.chip-strong { color: #ef4444; }
  &.chip-normal { color: #f97316; }
  &.chip-weak { color: #6b7280; }
}
.mt-12 { margin-top: 12px; }
.mt-16 { margin-top: 16px; }
.fund-flow-note {
  margin-bottom: 10px;
  padding: 10px 12px;
  font-size: 12px;
  line-height: 1.6;
  color: var(--claw-text-secondary);
  background: rgba(59, 130, 246, 0.06);
  border: 1px solid rgba(59, 130, 246, 0.12);
  border-radius: 10px;
}

// V2.2融合: 筹码分析卡片
.chip-card {
  background: var(--claw-card-bg);
  border-radius: 14px;
  padding: 16px;
  margin-bottom: 12px;
}
.chip-metrics-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin: 12px 0;
}
.chip-metric {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid rgba(148, 163, 184, 0.18);
  background: linear-gradient(180deg, rgba(255,255,255,0.03), transparent);
}
.chip-metric-label {
  font-size: 11px;
  color: var(--claw-text-muted);
}
.chip-metric-value {
  font-size: 15px;
  font-weight: 700;
  color: var(--claw-text-primary);
}
.chip-levels {
  display: flex;
  gap: 16px;
  margin-top: 8px;
}
.chip-level-row {
  display: flex;
  align-items: center;
  gap: 6px;
}
.level-label {
  font-size: 12px;
  color: var(--claw-text-muted);
  white-space: nowrap;
}
.level-tags {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}
.chip-desc {
  margin-top: 8px;
  font-size: 13px;
  color: var(--claw-text-secondary);
  line-height: 1.5;
}

// V2.2融合: 量化交易信号列表
.signal-list-card {
  background: var(--claw-card-bg);
  border-radius: 14px;
  padding: 16px;
  margin-bottom: 12px;
}
.signal-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.signal-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.03);
}
.signal-text {
  font-size: 13px;
  color: var(--claw-text-primary);
}
.orderbook-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;
}
.orderbook-hint {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 12px;
  font-size: 12px;
  color: var(--claw-text-secondary);
}

// K线工具栏
.kline-toolbar {
  display: flex;
  align-items: center;
  gap: 20px;
  padding: 10px 14px 9px;
  background: var(--claw-card-bg, #111827);
  border: 1px solid var(--claw-border, rgba(226,232,240,0.9));
  border-bottom: 0;
  border-radius: 14px 14px 0 0;
  flex-wrap: wrap;
  margin-bottom: 0;
  box-shadow: inset 0 -1px 0 rgba(148,163,184,0.12);
}
.kline-indicator-group {
  display: flex;
  align-items: center;
  gap: 6px;
}
.toolbar-label {
  font-size: 12px;
  color: #667085;
  white-space: nowrap;
  font-weight: 500;
}
.kline-legend {
  display: flex;
  gap: 12px;
  margin-left: auto;
  font-size: 11px;
  color: #667085;
  flex-wrap: wrap;
  b { font-weight: 600; }

  span { white-space: nowrap; }
  .is-focus-mode {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(148, 163, 184, 0.12);
    color: #475569;
    &.is-hovering {
      background: rgba(59, 130, 246, 0.12);
      color: #2563eb;
    }
  }
  .is-volume {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(251, 191, 36, 0.12);
    color: #b45309;
  }
  .is-volume-light {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 3px 8px;
    border-radius: 999px;
    background: rgba(59, 130, 246, 0.08);
    color: #475569;
  }
}
.kline-loading {
  height: 600px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #667085;
  font-size: 14px;
}
.kline-panel {
  position: relative;
  padding: 10px 14px 12px;
  margin-top: -1px;
  border-top-left-radius: 0;
  border-top-right-radius: 0;
  outline: none; // 去掉focus边框
  &:focus-visible {
    box-shadow: 0 0 0 2px rgba(59,130,246,0.3);
    border-radius: 0 0 14px 14px;
  }
  // 确保ECharts canvas正确处理滚轮和触摸事件
  :deep(canvas) {
    touch-action: none; // 防止浏览器拦截触摸/滚轮手势
    image-rendering: high-quality;
  }
}
.kline-side-panel {
  position: absolute;
  top: 14px;
  right: 16px;
  z-index: 5;
  width: 210px;
  padding: 10px 12px;
  border-radius: 14px;
  border: 1px solid rgba(148, 163, 184, 0.14);
  background: linear-gradient(180deg, rgba(255, 255, 255, 0.94), rgba(248, 250, 252, 0.92));
  box-shadow: 0 10px 22px rgba(15, 23, 42, 0.08);
  backdrop-filter: blur(10px);
  pointer-events: none;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.kline-side-panel-head {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.kline-side-panel-date {
  display: flex;
  align-items: center;
  gap: 7px;
}
.kline-side-panel-mode {
  display: inline-flex;
  align-self: flex-start;
  padding: 2px 7px;
  border-radius: 999px;
  background: rgba(148, 163, 184, 0.12);
  color: #64748b;
  font-size: 10px;
  font-weight: 600;
  &.is-hovering {
    background: rgba(59, 130, 246, 0.12);
    color: #2563eb;
  }
}
.kline-side-panel-date-text {
  font-size: 11px;
  font-weight: 700;
  color: #475569;
}
.kline-side-panel-price-line {
  display: flex;
  align-items: baseline;
  gap: 7px;
}
.kline-side-panel-price {
  font-size: 18px;
  line-height: 1;
  font-weight: 700;
  letter-spacing: -0.03em;
}
.kline-side-panel-change {
  font-size: 12px;
  font-weight: 700;
}
.kline-side-panel-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 8px;
  font-size: 10px;
  color: #64748b;
}
.kline-side-panel-divider {
  height: 1px;
  background: rgba(148, 163, 184, 0.16);
  margin: 1px 0;
}
.kline-side-panel-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 5px 10px;
  font-size: 10px;
  color: #475569;
}
.kline-side-panel-key {
  color: #94a3b8;
}
.kline-shortcuts {
  text-align: center;
  font-size: 10px;
  color: #475569;
  padding-top: 8px;
  letter-spacing: 0.5px;
  user-select: none;
}

@media (max-width: 1180px) {
  .stock-header {
    grid-template-columns: minmax(0, 1fr) auto;
  }

  .stock-info {
    grid-column: 1;
    grid-row: 1;
  }

  .score-badges {
    grid-column: 2;
    grid-row: 1;
  }

  .market-snapshot {
    grid-column: 1 / -1;
    grid-row: 2;
    padding: 14px 0 0;
    border-top: 1px solid var(--claw-border-light);
    border-right: 0;
    border-left: 0;
  }

  .hero-metrics {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }

  .hero-metric:nth-child(4) {
    border-left: 0;
  }
}

@media (max-width: 768px) {
  .stock-hero {
    padding: 15px;
    border-radius: 13px;
  }

  .detail-context {
    margin-bottom: 12px;
  }

  .quote-status {
    display: none;
  }

  .stock-header {
    display: flex;
    flex-direction: column;
    gap: 16px;
    margin-bottom: 15px;
  }

  .stock-title-row h2 {
    font-size: 21px;
  }

  .current-price {
    font-size: 30px;
  }

  .market-snapshot {
    width: 100%;
    padding-top: 14px;
  }

  .score-badges {
    width: 100%;
  }

  .badge {
    flex: 1;
    width: auto;
  }

  .hero-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .hero-metric + .hero-metric {
    border-left: 0;
  }

  .hero-metric:nth-child(even) {
    border-left: 1px solid var(--claw-border-light);
  }

  .hero-metric:nth-child(n + 3) {
    border-top: 1px solid var(--claw-border-light);
  }

  .detail-tabs {
    margin-top: 12px;

    :deep(.el-tabs__header) {
      top: 50px;
      margin-bottom: 12px;
      padding: 0 4px;
      border-radius: 10px;
    }

    :deep(.el-tabs__item) {
      height: 48px;
      padding: 0 14px;
      font-size: 13px;
    }
  }

  .card-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 12px;

    :deep(.el-card__body) {
      padding: 15px;
    }
  }

  .score-dual {
    grid-template-columns: 1fr;
  }

  .kline-toolbar {
    gap: 8px;
  }

  .kline-legend {
    display: none;
  }

  .kline-side-panel {
    display: none;
  }
}

/* 操作预案策略卡片 */
.plan-data-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 14px;
  margin-bottom: 10px;
  color: var(--claw-text-secondary);
  font-size: 12px;
}
.plan-quality-alert {
  margin-bottom: 12px;
}
.plan-header {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 16px;
  padding: 12px;
  background: var(--neutral-50);
  border-radius: 8px;
}
.plan-header-item {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.plan-label {
  color: var(--claw-text-secondary);
  font-size: 12px;
}
.plan-val {
  font-weight: 700;
  font-size: 16px;
}
.tech-tag {
  font-size: 11px;
  padding: 2px 8px;
  background: rgba(var(--el-color-primary-rgb), 0.1);
  border-radius: 4px;
  color: var(--el-color-primary);
}
.plan-strategies {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 16px;
}
.strategy-card {
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--claw-border);
  background: var(--el-bg-color);
}
.strategy-card.trend {
  border-left: 3px solid var(--el-color-primary);
}
.strategy-card.aggressive {
  border-left: 3px solid var(--el-color-danger);
}
.strategy-card.avoid {
  border-left: 3px solid var(--el-color-info);
  background: rgba(var(--el-color-info-rgb), 0.05);
}
.strategy-card.watch {
  border-left: 3px solid var(--el-color-warning);
}
.strategy-card-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.confidence-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  font-weight: 600;
}
.confidence-badge.高 {
  background: rgba(34, 197, 94, 0.15);
  color: #22c55e;
}
.confidence-badge.中 {
  background: rgba(234, 179, 8, 0.15);
  color: #eab308;
}
.confidence-badge.低 {
  background: rgba(239, 68, 68, 0.15);
  color: #ef4444;
}
.strategy-invalidation {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed var(--claw-border);
  font-size: 12px;
}
.inv-label {
  color: var(--claw-text-secondary);
  margin-right: 4px;
}
.avoid-info {
  padding: 8px;
  font-size: 13px;
}
.plan-sr-section {
  margin-bottom: 12px;
}
.sr-src {
  font-size: 10px;
  color: var(--claw-text-secondary);
  margin-left: 4px;
}
.plan-avoid-section {
  margin-bottom: 12px;
}
.plan-signals-section {
  margin-bottom: 12px;
}
.signal-list {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
}
.pct-label {
  font-size: 11px;
  color: var(--claw-text-secondary);
  margin-left: 2px;
}
.text-red { color: #ef4444; }
.text-green { color: #22c55e; }
.sentiment-badge {
  font-size: 11px;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(var(--el-color-warning-rgb), 0.1);
  color: var(--el-color-warning);
}
</style>
