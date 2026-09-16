<template>
  <div class="page-container">
    <div class="page-shell model-lab-page">
      <div class="page-hero">
        <div>
          <h2 class="page-title"><el-icon class="title-icon"><DataAnalysis /></el-icon>模型实验室</h2>
          <div class="page-subtitle">时间外走步验证、概率校准、不可变快照回放与 Champion / Challenger 治理</div>
        </div>
        <div class="identity-chips">
          <el-tag effect="dark">基础冠军 {{ identity.champion_model_version || '--' }}</el-tag>
          <el-tag :type="identity.runtime_mode === 'legacy' ? 'info' : 'warning'">{{ identity.runtime_mode || 'legacy' }}</el-tag>
          <el-tag v-for="lane in deploymentLanes" :key="lane.target_board" :type="lane.effective_active ? 'warning' : lane.active ? 'danger' : 'info'">
            T{{ lane.target_board }} {{ lane.effective_active ? `人工覆盖 ${lane.active_model_version}` : lane.active ? '产物校验失败·Legacy回退' : 'Legacy' }}
          </el-tag>
        </div>
      </div>

      <el-alert
        title="训练不会自动替换生产模型"
        type="info"
        :closable="false"
        description="挑战者必须先通过走步验证，再进入影子运行；只有人工审批后才能晋级。"
        show-icon
      />

      <section class="panel-card regime-panel">
        <div class="regime-header">
          <div>
            <div class="panel-title"><el-icon><Compass /></el-icon>市场风格状态</div>
            <div class="regime-contract">只使用该交易日收盘前可见数据；风格用于分层评估，不触发每日自动改参。</div>
          </div>
          <el-button type="primary" plain :loading="classifyingRegime" @click="runRegimeClassification">生成盘后风格快照</el-button>
        </div>
        <template v-if="currentRegime">
          <div class="regime-summary">
            <el-tag effect="dark" size="large">{{ currentRegime.primary_regime_name }}</el-tag>
            <span>{{ currentRegime.trade_date }}</span>
            <span>置信度 {{ pct(currentRegime.confidence) }}</span>
            <el-tag :type="currentRegime.quality_status === 'good' ? 'success' : 'warning'" size="small">数据{{ currentRegime.quality_status }}</el-tag>
            <el-tag v-if="currentRegime.transition_type !== 'unchanged'" type="warning" size="small">{{ currentRegime.transition_type }}</el-tag>
          </div>
          <div class="regime-body">
            <div class="regime-scores">
              <div v-for="item in regimeScores" :key="item.code" class="score-row">
                <span>{{ regimeName(item.code) }}</span>
                <el-progress :percentage="Math.round(item.score)" :stroke-width="8" :show-text="false" />
                <strong>{{ Number(item.score).toFixed(1) }}</strong>
              </div>
            </div>
            <ul class="evidence-list"><li v-for="item in currentRegime.evidence || []" :key="item">{{ item }}</li></ul>
          </div>
        </template>
        <el-empty v-else description="尚无盘后市场风格快照" :image-size="64" />
      </section>

      <div class="lab-grid">
        <section class="panel-card train-panel">
          <div class="panel-title"><el-icon><Cpu /></el-icon>训练挑战者</div>
          <el-form :model="trainForm" label-width="104px" size="small">
            <el-form-item label="数据集">
              <el-select v-model="trainForm.dataset_source" style="width:220px">
                <el-option label="历史K线面板（预训练）" value="historical_panel" />
                <el-option label="冻结预测快照（晋级验收）" value="prediction_snapshots" />
              </el-select>
            </el-form-item>
            <el-form-item label="预测赛道">
              <el-radio-group v-model="trainForm.target_board">
                <el-radio-button :value="1">次日首板</el-radio-button>
                <el-radio-button :value="2">首板晋二板</el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item v-if="trainForm.dataset_source === 'historical_panel'" label="历史交易日">
              <el-input-number v-model="trainForm.historical_lookback_days" :min="80" :max="1500" />
            </el-form-item>
            <el-form-item v-if="trainForm.dataset_source === 'historical_panel'" label="每日宽召回">
              <el-input-number v-model="trainForm.historical_candidate_limit" :min="50" :max="1500" />
            </el-form-item>
            <el-form-item label="初始训练日">
              <el-input-number v-model="trainForm.initial_train_days" :min="10" :max="1000" />
            </el-form-item>
            <el-form-item label="验证窗口">
              <el-input-number v-model="trainForm.validation_days" :min="1" :max="30" />
            </el-form-item>
            <el-form-item label="校准尾窗">
              <el-input-number v-model="trainForm.calibration_days" :min="3" :max="30" />
            </el-form-item>
            <el-form-item label="保存产物">
              <el-switch v-model="trainForm.persist" />
              <span class="form-help">关闭时只做只读演练</span>
            </el-form-item>
            <el-form-item>
              <el-button type="primary" :loading="training" @click="runTraining">
                {{ trainForm.persist ? '训练并登记挑战者' : '运行只读走步验证' }}
              </el-button>
            </el-form-item>
          </el-form>
        </section>

        <section class="panel-card result-panel">
          <div class="panel-title"><el-icon><TrendCharts /></el-icon>最近评估</div>
          <template v-if="latestResult">
            <div class="result-status">
              <el-tag :type="acceptanceStatus.type" effect="dark">
                {{ acceptanceStatus.label }}
              </el-tag>
              <span>{{ latestResult.model_version }}</span>
            </div>
            <div class="metric-grid">
              <div class="metric-card"><span>验证交易日</span><strong>{{ latestResult.walk_forward?.validation_trade_day_count ?? '--' }}</strong></div>
              <div class="metric-card"><span>正样本</span><strong>{{ challengerMetrics.positive_count ?? '--' }}</strong></div>
              <div class="metric-card"><span>挑战者 PR-AUC</span><strong>{{ pct(challengerMetrics.average_precision) }}</strong></div>
              <div class="metric-card"><span>冠军 PR-AUC</span><strong>{{ pct(championMetrics.average_precision) }}</strong></div>
              <div class="metric-card"><span>挑战者 Brier</span><strong>{{ number(challengerMetrics.brier_score) }}</strong></div>
              <div class="metric-card"><span>Top12 精度</span><strong>{{ pct(challengerMetrics.daily_rank?.['12']?.precision) }}</strong></div>
            </div>
            <el-table :data="latestResult.acceptance?.checks || []" size="small" stripe>
              <el-table-column prop="name" label="晋级闸门" min-width="170" />
              <el-table-column label="结果" width="90"><template #default="{ row }"><el-tag size="small" :type="row.passed ? 'success' : 'danger'">{{ row.passed ? '通过' : '未通过' }}</el-tag></template></el-table-column>
              <el-table-column prop="actual" label="实际" width="110" />
              <el-table-column prop="required" label="要求" width="110" />
              <el-table-column prop="detail" label="解释" min-width="240" show-overflow-tooltip />
            </el-table>
          </template>
          <el-empty v-else description="尚未运行训练评估" />
        </section>
      </div>

      <section class="panel-card shadow-control-panel">
        <div class="shadow-control-header">
          <div>
            <div class="panel-title"><el-icon><View /></el-icon>影子运行与人工晋级</div>
            <div class="regime-contract">挑战者只在同一冻结候选集上配对打分；至少积累 30 个交易日、50 个正样本、2 个可评估风格，且交易日配对 Bootstrap 的核心增益置信下界不为负。全部闸门通过后仍需人工确认，召回与交易闸门不会被绕过。</div>
          </div>
          <el-tag type="info">永不自动晋级</el-tag>
        </div>
        <div class="shadow-control-grid">
          <el-select v-model="shadowForm.artifact_id" placeholder="选择可影子运行产物" filterable>
            <el-option v-for="item in shadowEligibleArtifacts" :key="item.id" :label="`${item.id} · ${item.model_version}`" :value="item.id" />
          </el-select>
          <el-select v-model="shadowForm.prediction_run_id" placeholder="选择冻结预测运行" filterable>
            <el-option v-for="item in predictionRuns" :key="item.id" :label="`${item.id} · ${item.reference_trade_date} · ${item.snapshot_context}`" :value="item.id" />
          </el-select>
          <el-radio-group v-model="shadowForm.target_board">
            <el-radio-button :value="1">首板</el-radio-button>
            <el-radio-button :value="2">二板</el-radio-button>
          </el-radio-group>
          <el-button type="primary" plain :loading="shadowRunning" :disabled="!shadowForm.artifact_id || !shadowForm.prediction_run_id" @click="runShadow">运行影子</el-button>
          <el-button type="success" plain :loading="shadowEvaluating" :disabled="!shadowForm.artifact_id" @click="evaluateShadow">结算累计证据</el-button>
          <el-button type="warning" :disabled="!latestEligibleEvaluation" @click="openGovernance('approve')">人工晋级</el-button>
          <el-button type="danger" plain :disabled="!activeTargetDeployment" @click="openGovernance('rollback')">回滚当前赛道</el-button>
        </div>
        <div class="deployment-lanes">
          <div v-for="lane in deploymentLanes" :key="lane.target_board" class="deployment-card">
            <strong>T{{ lane.target_board }} {{ lane.target_board === 1 ? '首板' : '二板' }}</strong>
            <el-tag size="small" :type="lane.effective_active ? 'warning' : lane.active ? 'danger' : 'info'">{{ lane.effective_active ? '人工覆盖生效' : lane.active ? '校验失败·已回退' : 'Legacy Champion' }}</el-tag>
            <span>{{ lane.effective_model_version || lane.active_model_version }}</span>
            <small>执行开关 {{ lane.execution_enabled ? '开启' : '关闭' }}；最新操作 {{ lane.latest_event?.action || '--' }}<template v-if="lane.integrity_error">；{{ lane.integrity_error }}</template></small>
          </div>
        </div>
      </section>

      <el-tabs v-model="activeTab" class="lab-tabs">
        <el-tab-pane label="风格历史" name="regimes">
          <el-table :data="regimeHistory" stripe size="small" empty-text="暂无风格快照">
            <el-table-column prop="trade_date" label="交易日" width="110" />
            <el-table-column prop="primary_regime_name" label="主风格" min-width="150"><template #default="{ row }"><el-tag size="small">{{ row.primary_regime_name }}</el-tag></template></el-table-column>
            <el-table-column prop="secondary_regime_name" label="次风格" min-width="140" />
            <el-table-column prop="confidence" label="置信度" width="100"><template #default="{ row }">{{ pct(row.confidence) }}</template></el-table-column>
            <el-table-column prop="transition_type" label="切换" width="110" />
            <el-table-column prop="quality_status" label="质量" width="90"><template #default="{ row }"><el-tag size="small" :type="row.quality_status === 'good' ? 'success' : 'warning'">{{ row.quality_status }}</el-tag></template></el-table-column>
            <el-table-column prop="regime_version" label="规则版本" min-width="220" />
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="训练记录" name="training">
          <el-table :data="trainingRuns" stripe size="small" empty-text="暂无训练记录">
            <el-table-column prop="id" label="#" width="70" />
            <el-table-column prop="target_board" label="赛道" width="90"><template #default="{ row }">{{ row.target_board === 1 ? '首板' : '二板' }}</template></el-table-column>
            <el-table-column prop="status" label="状态" width="100"><template #default="{ row }"><el-tag size="small" :type="statusTag(row.status)">{{ row.status }}</el-tag></template></el-table-column>
            <el-table-column prop="model_version" label="模型版本" min-width="260"><template #default="{ row }">{{ row.model_version || '--' }}</template></el-table-column>
            <el-table-column label="验收" width="110"><template #default="{ row }"><el-tag v-if="row.acceptance?.decision" size="small" :type="row.acceptance?.passed ? 'success' : 'danger'">{{ row.acceptance.decision }}</el-tag><span v-else>--</span></template></el-table-column>
            <el-table-column prop="started_at" label="开始时间" min-width="180" />
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="模型产物" name="artifacts">
          <el-table :data="artifacts" stripe size="small" empty-text="暂无模型产物">
            <el-table-column prop="model_version" label="版本" min-width="280" />
            <el-table-column prop="algorithm" label="算法" width="180" />
            <el-table-column prop="status" label="状态" width="120"><template #default="{ row }"><el-tag size="small" :type="statusTag(row.status)">{{ row.status }}</el-tag></template></el-table-column>
            <el-table-column prop="training_start_date" label="训练开始" width="110" />
            <el-table-column prop="training_end_date" label="训练结束" width="110" />
            <el-table-column prop="artifact_uri" label="产物路径" min-width="260" show-overflow-tooltip />
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="预测台账" name="runs">
          <el-table :data="predictionRuns" stripe size="small" empty-text="暂无正式调度快照" @row-click="openRun">
            <el-table-column prop="id" label="#" width="70" />
            <el-table-column prop="reference_trade_date" label="交易日" width="110" />
            <el-table-column prop="snapshot_context" label="时点" width="150" />
            <el-table-column prop="model_version" label="模型版本" min-width="250" />
            <el-table-column prop="candidate_count" label="候选" width="80" align="right" />
            <el-table-column prop="ranked_count" label="主榜" width="80" align="right" />
            <el-table-column prop="actionable_count" label="可执行" width="80" align="right" />
            <el-table-column prop="as_of_at" label="As-of" min-width="180" />
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="影子运行" name="shadow-runs">
          <el-table :data="shadowRuns" stripe size="small" empty-text="暂无影子运行">
            <el-table-column prop="id" label="#" width="70" />
            <el-table-column prop="reference_trade_date" label="预测日" width="110" />
            <el-table-column prop="target_board" label="赛道" width="80"><template #default="{ row }">T{{ row.target_board }}</template></el-table-column>
            <el-table-column prop="snapshot_context" label="时点" width="140" />
            <el-table-column prop="champion_model_version" label="Champion" min-width="220" show-overflow-tooltip />
            <el-table-column prop="challenger_model_version" label="Challenger" min-width="260" show-overflow-tooltip />
            <el-table-column prop="candidate_count" label="同池样本" width="90" align="right" />
            <el-table-column prop="status" label="状态" width="100"><template #default="{ row }"><el-tag size="small" type="success">{{ row.status }}</el-tag></template></el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="影子验收" name="shadow-evaluations">
          <el-table :data="shadowEvaluations" stripe size="small" empty-text="暂无可结算影子证据">
            <el-table-column prop="outcome_end_date" label="结果截止" width="110" />
            <el-table-column prop="target_board" label="赛道" width="75"><template #default="{ row }">T{{ row.target_board }}</template></el-table-column>
            <el-table-column prop="challenger_model_version" label="挑战者" min-width="260" show-overflow-tooltip />
            <el-table-column prop="trade_day_count" label="交易日" width="80" align="right" />
            <el-table-column prop="positive_count" label="正样本" width="80" align="right" />
            <el-table-column label="Challenger PR-AUC" width="145"><template #default="{ row }">{{ pct(row.metrics?.challenger_metrics?.average_precision) }}</template></el-table-column>
            <el-table-column label="Champion PR-AUC" width="135"><template #default="{ row }">{{ pct(row.metrics?.champion_metrics?.average_precision) }}</template></el-table-column>
            <el-table-column label="PR增益90%下界" width="145"><template #default="{ row }">{{ pct(row.metrics?.paired_bootstrap?.average_precision_delta?.lower) }}</template></el-table-column>
            <el-table-column label="Brier改善90%下界" width="155"><template #default="{ row }">{{ pct(row.metrics?.paired_bootstrap?.brier_improvement?.lower) }}</template></el-table-column>
            <el-table-column label="Top12增益90%下界" width="160"><template #default="{ row }">{{ pct(row.metrics?.paired_bootstrap?.top12_precision_delta?.lower) }}</template></el-table-column>
            <el-table-column prop="decision" label="决策" min-width="170" fixed="right"><template #default="{ row }"><el-tag size="small" :type="decisionTag(row.decision)">{{ decisionLabel(row.decision) }}</el-tag></template></el-table-column>
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="晋级与回滚审计" name="deployments">
          <el-table :data="deploymentEvents" stripe size="small" empty-text="尚无人工晋级/回滚事件">
            <el-table-column prop="created_at" label="时间" min-width="170" />
            <el-table-column prop="target_board" label="赛道" width="75"><template #default="{ row }">T{{ row.target_board }}</template></el-table-column>
            <el-table-column prop="action" label="动作" width="90"><template #default="{ row }"><el-tag size="small" :type="row.action === 'rollback' ? 'danger' : 'warning'">{{ row.action }}</el-tag></template></el-table-column>
            <el-table-column prop="from_model_version" label="从" min-width="210" show-overflow-tooltip />
            <el-table-column prop="to_model_version" label="到" min-width="240" show-overflow-tooltip />
            <el-table-column prop="operator" label="操作人" width="100" />
            <el-table-column prop="reason" label="原因" min-width="240" show-overflow-tooltip />
          </el-table>
        </el-tab-pane>

        <el-tab-pane label="影子实验登记" name="experiments">
          <el-table :data="experiments" stripe size="small" empty-text="暂无通过验收的影子实验">
            <el-table-column prop="name" label="实验" min-width="240" />
            <el-table-column prop="champion_model_version" label="Champion" min-width="240" />
            <el-table-column prop="challenger_model_version" label="Challenger" min-width="280" />
            <el-table-column prop="allocation_mode" label="模式" width="100" />
            <el-table-column prop="status" label="状态" width="100" />
            <el-table-column prop="decision" label="决策" width="100"><template #default="{ row }">{{ row.decision || '--' }}</template></el-table-column>
          </el-table>
        </el-tab-pane>
      </el-tabs>
    </div>

    <el-dialog v-model="runDialogVisible" title="不可变预测快照回放" width="82%">
      <el-descriptions v-if="runDetail.run" :column="3" size="small" border>
        <el-descriptions-item label="运行ID">{{ runDetail.run.id }}</el-descriptions-item>
        <el-descriptions-item label="时点">{{ runDetail.run.snapshot_context }}</el-descriptions-item>
        <el-descriptions-item label="数据版本">{{ runDetail.run.data_version }}</el-descriptions-item>
      </el-descriptions>
      <el-table :data="runDetail.snapshots || []" stripe size="small" style="margin-top:16px" max-height="520">
        <el-table-column prop="rank_position" label="排名" width="70"><template #default="{ row }">{{ row.rank_position || '--' }}</template></el-table-column>
        <el-table-column prop="code" label="代码" width="90" />
        <el-table-column prop="name" label="名称" width="100" />
        <el-table-column prop="target_board" label="目标" width="70" />
        <el-table-column prop="candidate_route" label="路线" min-width="180" />
        <el-table-column label="概率" width="100"><template #default="{ row }">{{ pct(row.calibrated_probability) }}</template></el-table-column>
        <el-table-column label="可执行" width="90"><template #default="{ row }"><el-tag size="small" :type="row.actionable ? 'success' : 'info'">{{ row.actionable ? '是' : '否' }}</el-tag></template></el-table-column>
      </el-table>
    </el-dialog>

    <el-dialog v-model="governanceDialogVisible" :title="governanceAction === 'approve' ? '人工晋级确认' : '回滚确认'" width="560px">
      <el-alert
        :title="governanceAction === 'approve' ? '该操作会让已验收挑战者覆盖所选赛道的概率排序' : '该操作会追加回滚事件并恢复上一版本或 Legacy'"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-form :model="governanceForm" label-width="100px" style="margin-top:18px">
        <el-form-item label="赛道"><strong>T{{ shadowForm.target_board }} {{ shadowForm.target_board === 1 ? '首板' : '二板' }}</strong></el-form-item>
        <el-form-item v-if="governanceAction === 'approve'" label="产物"><span class="break-text">{{ selectedArtifact?.model_version || '--' }}</span></el-form-item>
        <el-form-item label="管理令牌">
          <el-input v-model="governanceToken" type="password" show-password autocomplete="off" />
          <small class="danger-help">仅保存在当前页面内存；后端必须配置 PROMOTION_GOVERNANCE_TOKEN。</small>
        </el-form-item>
        <el-form-item label="操作主体"><strong>由后端 PROMOTION_GOVERNANCE_OPERATOR 确认</strong></el-form-item>
        <el-form-item label="原因"><el-input v-model="governanceForm.reason" type="textarea" :rows="3" maxlength="2000" show-word-limit /></el-form-item>
        <el-form-item label="确认短语">
          <el-input v-model="governanceForm.confirmation_phrase" :placeholder="requiredConfirmation" />
          <small class="danger-help">必须完整输入 {{ requiredConfirmation }}，系统不会代填。</small>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="governanceDialogVisible = false">取消</el-button>
        <el-button :type="governanceAction === 'approve' ? 'warning' : 'danger'" :loading="governanceSubmitting" @click="submitGovernance">确认追加审计事件</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import {
  approvePromotionDeployment,
  classifyMarketRegime,
  evaluatePromotionShadow,
  getMarketRegimeHistory,
  getPromotionDeployments,
  getPromotionExperiments,
  getPromotionModelArtifacts,
  getPromotionModelIdentity,
  getPromotionPredictionRun,
  getPromotionPredictionRuns,
  getPromotionShadowEvaluations,
  getPromotionShadowRuns,
  getPromotionTrainingRuns,
  rollbackPromotionDeployment,
  runPromotionShadow,
  trainPromotionChallenger,
} from '@/api'
import { notifySuccess, notifyWarning } from '@/utils/message'

const activeTab = ref('regimes')
const identity = ref({})
const artifacts = ref([])
const experiments = ref([])
const predictionRuns = ref([])
const trainingRuns = ref([])
const regimeHistory = ref([])
const currentRegime = ref(null)
const latestResult = ref(null)
const training = ref(false)
const classifyingRegime = ref(false)
const runDialogVisible = ref(false)
const runDetail = ref({})
const shadowRuns = ref([])
const shadowEvaluations = ref([])
const deploymentData = ref({ lanes: [], events: [] })
const shadowRunning = ref(false)
const shadowEvaluating = ref(false)
const governanceDialogVisible = ref(false)
const governanceSubmitting = ref(false)
const governanceAction = ref('approve')
const governanceToken = ref('')
const governanceForm = ref({ reason: '', confirmation_phrase: '', operation_id: '', expected_current_event_id: null })
const shadowForm = ref({ artifact_id: null, prediction_run_id: null, target_board: 1, snapshot_context: 'promotion_2000' })
const trainForm = ref({
  target_board: 1,
  dataset_source: 'historical_panel',
  historical_lookback_days: 120,
  historical_candidate_limit: 450,
  initial_train_days: 60,
  validation_days: 10,
  step_days: 10,
  calibration_days: 10,
  snapshot_context: 'promotion_2000',
  persist: false,
})

const challengerMetrics = computed(() => latestResult.value?.walk_forward?.challenger_metrics || {})
const championMetrics = computed(() => latestResult.value?.walk_forward?.champion_metrics || {})
const acceptanceStatus = computed(() => {
  const acceptance = latestResult.value?.acceptance || {}
  if (acceptance.decision === 'pretraining_only') return { type: 'info', label: '仅预训练，不具备晋级资格' }
  return acceptance.passed
    ? { type: 'success', label: '可进入影子运行' }
    : { type: 'danger', label: '拒绝晋级' }
})
const deploymentLanes = computed(() => deploymentData.value.lanes?.length ? deploymentData.value.lanes : (identity.value.deployments || []))
const deploymentEvents = computed(() => deploymentData.value.events || [])
const shadowEligibleArtifacts = computed(() => artifacts.value.filter((item) => ['shadow_eligible', 'approved', 'active'].includes(item.status)))
const selectedArtifact = computed(() => artifacts.value.find((item) => item.id === shadowForm.value.artifact_id))
const latestEligibleEvaluation = computed(() => shadowEvaluations.value.find((item) => (
  item.artifact_id === shadowForm.value.artifact_id
  && item.target_board === shadowForm.value.target_board
  && item.snapshot_context === shadowForm.value.snapshot_context
  && item.decision === 'manual_review_eligible'
  && item.acceptance?.passed
)))
const selectedTargetDeployment = computed(() => deploymentLanes.value.find((item) => item.target_board === shadowForm.value.target_board))
const activeTargetDeployment = computed(() => selectedTargetDeployment.value?.active ? selectedTargetDeployment.value : null)
const requiredConfirmation = computed(() => governanceAction.value === 'approve' ? 'APPROVE_CHAMPION' : 'ROLLBACK_CHAMPION')
const decisionTag = (decision) => decision === 'manual_review_eligible' ? 'success' : decision === 'shadow_rejected' ? 'danger' : 'warning'
const decisionLabel = (decision) => ({ manual_review_eligible: '可人工审批', shadow_rejected: '影子拒绝', collecting: '继续积累证据' }[decision] || decision)
const regimeNames = {
  risk_off: '退潮/风险规避', recovery: '冰点修复', sector_rotation: '板块轮动',
  sector_maintrend: '板块主升', individual_maintrend: '个股独立主升',
  high_board_speculation: '高标投机', broad_trend: '普涨趋势', balanced: '均衡震荡',
}
const regimeName = (code) => regimeNames[code] || code
const regimeScores = computed(() => Object.entries(currentRegime.value?.scores || {})
  .map(([code, score]) => ({ code, score: Number(score) || 0 }))
  .sort((a, b) => b.score - a.score))
const pct = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? `${(Number(value) * 100).toFixed(2)}%` : '--'
const number = (value) => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value)) ? Number(value).toFixed(4) : '--'
const statusTag = (status) => {
  if (['completed', 'champion', 'shadow_eligible', 'ready'].includes(status)) return 'success'
  if (['failed', 'rejected'].includes(status)) return 'danger'
  if (['running', 'shadow'].includes(status)) return 'warning'
  return 'info'
}

async function loadData() {
  const [id, ar, ex, pr, tr, regimes, sr, se, deployments] = await Promise.allSettled([
    getPromotionModelIdentity(),
    getPromotionModelArtifacts(),
    getPromotionExperiments(),
    getPromotionPredictionRuns({ limit: 50 }),
    getPromotionTrainingRuns({ limit: 30 }),
    getMarketRegimeHistory({ limit: 120 }),
    getPromotionShadowRuns({ limit: 100 }),
    getPromotionShadowEvaluations({ limit: 100 }),
    getPromotionDeployments(),
  ])
  if (id.status === 'fulfilled') identity.value = id.value
  if (ar.status === 'fulfilled') {
    artifacts.value = ar.value.artifacts || []
    if (!shadowForm.value.artifact_id) shadowForm.value.artifact_id = artifacts.value.find((item) => ['shadow_eligible', 'approved', 'active'].includes(item.status))?.id || null
  }
  if (ex.status === 'fulfilled') experiments.value = ex.value.experiments || []
  if (pr.status === 'fulfilled') {
    predictionRuns.value = pr.value.runs || []
    if (!shadowForm.value.prediction_run_id) shadowForm.value.prediction_run_id = predictionRuns.value.find((item) => item.snapshot_source === 'schedule')?.id || predictionRuns.value[0]?.id || null
  }
  if (tr.status === 'fulfilled') {
    trainingRuns.value = tr.value.training_runs || []
    const latest = trainingRuns.value.find((item) => item.status === 'completed')
    if (latest) latestResult.value = { model_version: latest.model_version, walk_forward: latest.metrics, acceptance: latest.acceptance }
  }
  if (regimes.status === 'fulfilled') {
    regimeHistory.value = regimes.value.snapshots || []
    currentRegime.value = regimeHistory.value[0] || null
  }
  if (sr.status === 'fulfilled') shadowRuns.value = sr.value.shadow_runs || []
  if (se.status === 'fulfilled') shadowEvaluations.value = se.value.evaluations || []
  if (deployments.status === 'fulfilled') deploymentData.value = deployments.value
}

watch(
  () => shadowForm.value.prediction_run_id,
  (runId) => {
    const run = predictionRuns.value.find((item) => item.id === runId)
    if (run?.snapshot_context) shadowForm.value.snapshot_context = run.snapshot_context
  },
)

async function runRegimeClassification() {
  classifyingRegime.value = true
  try {
    const result = await classifyMarketRegime({ snapshot_context: 'postmarket', persist: true })
    currentRegime.value = result
    notifySuccess(`已冻结 ${result.trade_date} 市场风格：${result.primary_regime_name}`)
    await loadData()
  } catch { /* API interceptor reports detail */ }
  finally { classifyingRegime.value = false }
}

async function runTraining() {
  training.value = true
  try {
    const result = await trainPromotionChallenger({ ...trainForm.value })
    latestResult.value = result
    if (result.acceptance?.passed) notifySuccess('挑战者通过离线验收，可进入影子运行')
    else notifyWarning('挑战者未通过全部验收闸门，生产冠军保持不变')
    await loadData()
    latestResult.value = result
  } catch { /* API interceptor reports detail */ }
  finally { training.value = false }
}

async function runShadow() {
  shadowRunning.value = true
  try {
    const result = await runPromotionShadow({
      prediction_run_id: shadowForm.value.prediction_run_id,
      artifact_id: shadowForm.value.artifact_id,
      persist: true,
    })
    shadowForm.value.target_board = result.target_board
    shadowForm.value.snapshot_context = result.snapshot_context
    activeTab.value = 'shadow-runs'
    notifySuccess(`影子运行已冻结：同池 ${result.candidate_count} 个候选，生产输出未改变`)
    await loadData()
  } catch { /* API interceptor reports detail */ }
  finally { shadowRunning.value = false }
}

async function evaluateShadow() {
  shadowEvaluating.value = true
  try {
    const result = await evaluatePromotionShadow({
      artifact_id: shadowForm.value.artifact_id,
      target_board: shadowForm.value.target_board,
      snapshot_context: shadowForm.value.snapshot_context,
      persist: true,
    })
    activeTab.value = 'shadow-evaluations'
    if (result.decision === 'manual_review_eligible') notifySuccess('累计影子闸门全部通过，可进入人工审批')
    else notifyWarning(result.decision === 'shadow_rejected' ? '样本充足但影子性能闸门失败' : '影子证据仍不足，继续积累交易日与正样本')
    await loadData()
  } catch { /* API interceptor reports detail */ }
  finally { shadowEvaluating.value = false }
}

function governanceOperationId(action) {
  const randomId = globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`
  return `${action}-${shadowForm.value.target_board}-${randomId}`
}

function openGovernance(action) {
  governanceAction.value = action
  governanceForm.value = {
    reason: '',
    confirmation_phrase: '',
    operation_id: governanceOperationId(action),
    expected_current_event_id: selectedTargetDeployment.value?.latest_event?.id ?? null,
  }
  governanceDialogVisible.value = true
}

async function submitGovernance() {
  governanceSubmitting.value = true
  try {
    if (governanceAction.value === 'approve') {
      await approvePromotionDeployment({
        artifact_id: shadowForm.value.artifact_id,
        target_board: shadowForm.value.target_board,
        snapshot_context: shadowForm.value.snapshot_context,
        governance_token: governanceToken.value,
        ...governanceForm.value,
      })
      notifySuccess('人工晋级事件已追加；后续同赛道正式快照启用概率覆盖层')
    } else {
      await rollbackPromotionDeployment({
        target_board: shadowForm.value.target_board,
        governance_token: governanceToken.value,
        ...governanceForm.value,
      })
      notifySuccess('回滚事件已追加，后续正式快照恢复上一版本或 Legacy')
    }
    governanceDialogVisible.value = false
    activeTab.value = 'deployments'
    await loadData()
  } catch { /* API interceptor reports detail */ }
  finally { governanceSubmitting.value = false }
}

async function openRun(row) {
  try {
    runDetail.value = await getPromotionPredictionRun(row.id)
    runDialogVisible.value = true
  } catch { /* ignore */ }
}

onMounted(loadData)
</script>

<style scoped lang="scss">
.model-lab-page { display: flex; flex-direction: column; gap: 18px; }
.identity-chips { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.regime-panel { min-width: 0; }
.regime-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.regime-contract { color: var(--claw-text-muted); font-size: 12px; margin-top: 5px; }
.regime-summary { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin: 14px 0; color: var(--claw-text-secondary); font-size: 13px; }
.regime-body { display: grid; grid-template-columns: minmax(360px, 1.2fr) minmax(300px, 1fr); gap: 24px; }
.regime-scores { display: flex; flex-direction: column; gap: 7px; }
.score-row { display: grid; grid-template-columns: 112px 1fr 44px; align-items: center; gap: 10px; font-size: 12px; color: var(--claw-text-secondary); }
.score-row strong { text-align: right; color: var(--claw-text-primary); }
.evidence-list { margin: 0; padding-left: 18px; color: var(--claw-text-secondary); font-size: 13px; line-height: 1.9; }
.lab-grid { display: grid; grid-template-columns: minmax(320px, 0.8fr) minmax(520px, 1.7fr); gap: 18px; }
.train-panel, .result-panel { min-width: 0; }
.form-help { margin-left: 10px; color: var(--claw-text-muted); font-size: 12px; }
.result-status { display: flex; align-items: center; gap: 10px; margin-bottom: 14px; color: var(--claw-text-secondary); font-size: 13px; overflow-wrap: anywhere; }
.metric-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; margin-bottom: 16px; }
.metric-card { padding: 12px; border: 1px solid var(--claw-border); border-radius: 12px; background: var(--claw-bg-soft); }
.metric-card span { display: block; color: var(--claw-text-muted); font-size: 12px; margin-bottom: 6px; }
.metric-card strong { color: var(--claw-text-primary); font-size: 18px; }
.shadow-control-panel { min-width: 0; }
.shadow-control-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.shadow-control-grid { display: grid; grid-template-columns: minmax(220px, 1.2fr) minmax(260px, 1.4fr) auto repeat(4, auto); gap: 10px; align-items: center; margin-top: 16px; }
.deployment-lanes { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-top: 14px; }
.deployment-card { display: grid; grid-template-columns: auto auto 1fr; gap: 8px 12px; align-items: center; padding: 12px; border: 1px solid var(--claw-border); border-radius: 12px; background: var(--claw-bg-soft); }
.deployment-card span { min-width: 0; overflow-wrap: anywhere; color: var(--claw-text-secondary); font-size: 12px; }
.deployment-card small { grid-column: 1 / -1; color: var(--claw-text-muted); }
.break-text { overflow-wrap: anywhere; }
.danger-help { display: block; margin-top: 5px; color: var(--el-color-danger); }
.lab-tabs { background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 16px; padding: 10px 16px 16px; }
:deep(.el-table) { border: 1px solid var(--claw-border); border-radius: 12px; overflow: hidden; }
@media (max-width: 1300px) { .shadow-control-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 1100px) { .lab-grid, .regime-body, .deployment-lanes { grid-template-columns: 1fr; } }
@media (max-width: 640px) {
  .metric-grid, .shadow-control-grid { grid-template-columns: repeat(2, 1fr); }
  .regime-header, .shadow-control-header { flex-direction: column; }
  .score-row { grid-template-columns: 96px 1fr 38px; }
}
</style>
