<template>
  <el-drawer :model-value="modelValue" title="AI 接入配置" size="min(520px, 100vw)" @update:model-value="emit('update:modelValue', $event)" destroy-on-close>
    <div class="connection-panel" v-loading="loading">
      <el-alert title="配置作用于全局 AI" description="新闻情感、事件和摘要等共用此接入。切换模型可能改变后续分析结果，不会重写历史新闻或修改交易规则。仅支持从本机工作台配置。" type="info" :closable="false" />
      <el-alert v-if="status.configuration_error" title="服务端配置无效或损坏，AI 已安全停用，请重新保存。" type="warning" :closable="false" />
      <el-alert v-if="loadError" :title="loadError" type="error" :closable="false" />
      <el-alert v-else-if="legacyBackend" :title="BACKEND_UPGRADE_MESSAGE" type="warning" :closable="false" />
      <el-button v-if="loadError" :loading="checking" @click="retryLoad">重新读取配置</el-button>
      <el-form label-position="top" @submit.prevent="save">
        <el-form-item label="启用 AI 分析">
          <el-switch v-model="form.enabled" active-text="启用" inactive-text="停用时保留规则清洗" />
        </el-form-item>
        <el-form-item label="接入方式">
          <el-radio-group v-model="form.auth_mode">
            <el-radio-button value="api_key">API Key</el-radio-button>
            <el-radio-button value="openai_oauth">OpenAI 账号授权</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <template v-if="form.auth_mode === 'api_key'">
          <el-form-item label="接口协议">
            <el-select v-model="form.api_format" style="width:100%">
              <el-option label="Anthropic 兼容 · 现有 MiniMax 接口" value="anthropic" />
              <el-option label="OpenAI 兼容 · Chat Completions" value="openai" />
            </el-select>
          </el-form-item>
          <el-form-item label="API 地址">
            <el-input v-model="form.base_url" placeholder="https://api.openai.com/v1" autocomplete="off" />
            <span class="form-help">填写服务根地址或 /v1 地址。更换地址需重新输入密钥，避免将旧密钥发送到新服务。</span>
          </el-form-item>
          <el-form-item label="模型名称">
            <el-input v-model="form.model" placeholder="填写服务商支持的模型 ID" />
          </el-form-item>
          <el-form-item label="API Key">
            <el-input v-model="form.api_key" type="password" show-password autocomplete="new-password" :placeholder="status.has_key ? '已保存密钥 · 留空保持不变' : '请输入 API Key'" />
            <el-checkbox v-if="status.has_key" v-model="form.clear_api_key">清除已保存密钥</el-checkbox>
            <span class="form-help">密钥仅保存到服务端，不回传明文，也不存入浏览器缓存。</span>
          </el-form-item>
          <div class="parameter-grid">
            <el-form-item label="最大输出 Token">
              <el-input-number v-model="form.max_tokens" :min="64" :max="32768" :step="256" />
            </el-form-item>
            <el-form-item label="温度">
              <el-input-number v-model="form.temperature" :min="0" :max="2" :step="0.1" :precision="1" />
            </el-form-item>
          </div>
        </template>
        <template v-else>
          <el-alert title="通过官方 Codex 账号授权接入" description="这不是 OpenAI Platform 的通用 OAuth API。由服务端 Codex App Server 管理登录和续期，使用账号的 Codex 权益及额度；不共享你其他工具的登录凭据。" type="info" :closable="false" />
          <div class="oauth-status">
            <el-tag :type="statusReady && status.oauth?.connected ? 'success' : 'warning'">{{ !statusReady ? '状态未确认' : status.oauth?.connected ? '已授权' : '未授权' }}</el-tag>
            <span>{{ statusReady ? status.oauth.message : '请先恢复配置接口，不能据此判断是否安装 CLI。' }}</span>
          </div>
          <div v-if="statusReady && status.oauth?.available === false && status.oauth?.login_status === 'unavailable'" class="setup-note">
            <strong>前置依赖：服务端安装官方 Codex CLI</strong>
            <p>当前未检测到可用 CLI。安装后请刷新状态，不需要填写或复制 OAuth Token。</p>
            <a href="https://developers.openai.com/codex/cli/" target="_blank" rel="noopener noreferrer">查看官方安装文档 ↗</a>
          </div>
          <el-alert v-if="statusReady && status.oauth?.connected && status.oauth?.chat_available === false" :title="status.oauth.chat_message || '当前账号通道暂不能分析新闻'" type="warning" :closable="false" />
          <div v-if="authorizing" class="setup-note" role="status">
            <template v-if="safeAuthUrl">
              <strong>下一步：打开官方页面完成登录</strong>
              <p>授权地址已生成，还需点击下方蓝色按钮。这里会等待登录结果，不会自动跳转。</p>
              <p v-if="login.user_code">一次性验证码：<strong>{{ login.user_code }}</strong></p>
            </template>
            <template v-else>
              <strong>服务端有一笔未完成的授权</strong>
              <p>当前页面没有授权链接，请回到发起授权的标签页继续；若链接已丢失，请手动取消授权后重新连接。</p>
            </template>
          </div>
          <div class="oauth-actions">
            <el-button v-if="authorizing && safeAuthUrl" tag="a" type="primary" :href="safeAuthUrl" target="_blank" rel="noopener noreferrer">打开 OpenAI 授权页面 ↗</el-button>
            <el-button v-else type="primary" :disabled="!!loginDisabledReason" :loading="oauthBusy" @click="startLogin">连接 OpenAI 账号</el-button>
            <el-button :loading="checking" @click="refreshStatus">刷新状态</el-button>
            <el-button v-if="authorizing" @click="cancelLogin">取消授权</el-button>
            <el-popconfirm v-if="status.oauth?.connected" title="断开本服务的 OpenAI 授权？后续请求将无法使用该账号。" @confirm="logout">
              <template #reference><el-button type="danger" plain>断开授权</el-button></template>
            </el-popconfirm>
          </div>
          <p v-if="loginDisabledReason && !authorizing" class="form-help" role="status">{{ loginDisabledReason }}</p>
          <p v-if="authorizing && safeAuthUrl" class="form-help">请允许打开新标签页，并在运行 Claw 的这台电脑上完成授权。成功后返回这里，保存配置才能切换分析通道。</p>
          <el-form-item label="账号模型（可选）">
            <el-select v-model="form.oauth_model" :empty-values="[null, undefined]" :value-on-clear="''" :disabled="saving" filterable allow-create clearable default-first-option :loading="modelsLoading" placeholder="选择模型或输入完整模型 ID" style="width:100%" @change="onModelChange" @clear="form.oauth_model = ''">
              <el-option label="账号默认模型（无需指定）" value="" />
              <el-option v-for="model in modelOptions" :key="model.id" :label="model.label + ' · ' + model.id + (model.is_default ? '（推荐默认）' : '')" :value="model.id" />
            </el-select>
            <el-button size="small" :loading="modelsLoading" :disabled="!statusReady || !status.oauth?.connected" @click="loadModels">刷新模型列表</el-button>
            <span v-if="modelsError" class="form-help" role="status">{{ modelsError }}</span>
            <span v-else class="form-help">模型目录来自 Codex，是否可调用仍以连接测试为准；也可手动输入模型 ID。留空使用账号默认模型，不会自动替你选择其他模型。</span>
            <span class="form-help">已保存模型：{{ status.oauth_model || '账号默认模型' }}。{{ (form.oauth_model || '').trim() !== (status.oauth_model || '') ? '当前选择尚未保存。' : '' }}</span>
            <span class="form-help">保存账号通道时保留原 API Key 配置；账号通道不使用 API 温度和 Token 参数。选择模型不会绕过上方的分析能力检查。</span>
          </el-form-item>
          <el-form-item label="思考强度（账号通道）" data-testid="reasoning-effort">
            <el-select v-model="form.oauth_reasoning_effort" :empty-values="[null, undefined]" :disabled="saving" style="width:100%" @change="reasoningNotice = ''">
              <el-option :label="defaultEffortLabel" value="" />
              <el-option v-for="effort in reasoningEfforts" :key="effort" :label="effort" :value="effort" />
            </el-select>
            <span class="form-help">已保存思考强度：{{ status.oauth_reasoning_effort || '模型默认' }}。{{ form.oauth_reasoning_effort !== (status.oauth_reasoning_effort || '') ? '当前强度尚未保存。' : '' }}</span>
            <span v-if="!reasoningKnown" class="form-help" role="status">思考强度能力未知（手动模型、目录不可用或旧目录未提供元数据）；模型默认可用，不会猜测支持列表。已有强度不会自动删除，请确认兼容性或显式选择模型默认。</span>
            <el-alert v-if="incompatibleEffort" title="当前强度未在所选模型的支持列表中；已保留原值，请显式选择支持的强度或模型默认后保存。" type="warning" :closable="false" />
            <span v-if="reasoningNotice" class="form-help" role="status">{{ reasoningNotice }}</span>
            <span class="form-help">强度由模型目录声明；留空不覆盖模型默认。更高强度可能增加延迟及额度消耗，保存后仅影响后续分析。</span>
          </el-form-item>
        </template>
      </el-form>
    </div>
    <template #footer>
      <el-alert v-if="feedback" class="drawer-feedback" :title="feedback.message" :type="feedback.ok ? 'success' : 'warning'" :closable="false" />
      <div v-if="loading || checking || !statusReady" class="form-help">保存暂不可用：{{ loading || checking ? '正在读取配置，请稍候。' : '请先恢复配置接口并刷新状态。' }}</div>
      <div class="drawer-footer">
        <el-button :loading="testing" :disabled="saving || loading || checking || !statusReady" @click="testConnection">测试已保存配置</el-button>
        <el-button type="primary" :loading="saving" :disabled="loading || checking || !statusReady" @click="save">保存配置</el-button>
      </div>
      <div class="form-help">连接测试会发送一条简短请求，可能消耗额度。</div>
    </template>
  </el-drawer>
</template>

<script setup>
import { computed, onBeforeUnmount, reactive, ref, watch } from 'vue'
import { getAIStatus, getAIOAuthModels, saveAIConfig, testAIConnection, startAIOAuth, cancelAIOAuth, logoutAIOAuth } from '@/api'

const props = defineProps({ modelValue: Boolean })
const emit = defineEmits(['update:modelValue', 'status'])
const status = ref({})
const form = reactive({ enabled: false, auth_mode: 'api_key', api_format: 'anthropic', base_url: '', model: '', oauth_model: '', oauth_reasoning_effort: '', api_key: '', clear_api_key: false, max_tokens: 2048, temperature: 0.3 })
const loading = ref(false)
const loadError = ref('')
const statusLoaded = ref(false)
const BACKEND_UPGRADE_MESSAGE = '后端尚未加载 AI 配置与 OAuth 接口。请安排重启现有后端服务，再刷新状态；仅刷新网页不会生效。'
const backendCompatible = computed(() => ['api_key', 'openai_oauth'].includes(status.value?.auth_mode)
  && ['anthropic', 'openai'].includes(status.value?.api_format)
  && typeof status.value?.oauth?.available === 'boolean')
const legacyBackend = computed(() => statusLoaded.value && !backendCompatible.value)
const statusReady = computed(() => statusLoaded.value && backendCompatible.value && !loadError.value)
const saving = ref(false)
const testing = ref(false)
const checking = ref(false)
const oauthBusy = ref(false)
const login = ref(null)
const feedback = ref(null)
const modelOptions = ref([])
const modelsLoading = ref(false)
const modelsLoaded = ref(false)
const modelsError = ref('')
const reasoningNotice = ref('')
const validEfforts = new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh'])
const selectedModel = computed(() => form.oauth_model.trim()
  ? modelOptions.value.find(model => model.id === form.oauth_model.trim())
  : modelOptions.value.find(model => model.is_default === true))
const reasoningKnown = computed(() => !modelsError.value && Array.isArray(selectedModel.value?.reasoning_efforts)
  && selectedModel.value.reasoning_efforts.some(effort => validEfforts.has(effort)))
const reasoningEfforts = computed(() => reasoningKnown.value
  ? [...new Set(selectedModel.value.reasoning_efforts.filter(effort => validEfforts.has(effort)))] : [])
const defaultEffortLabel = computed(() => '模型默认' + (reasoningKnown.value && selectedModel.value?.default_reasoning_effort
  ? '（' + selectedModel.value.default_reasoning_effort + '）' : ''))
const incompatibleEffort = computed(() => !!form.oauth_reasoning_effort && reasoningKnown.value
  && !reasoningEfforts.value.includes(form.oauth_reasoning_effort))
function onModelChange() {
  reasoningNotice.value = ''
  if (incompatibleEffort.value) {
    const previous = form.oauth_reasoning_effort
    form.oauth_reasoning_effort = ''
    reasoningNotice.value = '切换模型后，原草稿强度 ' + previous + ' 不受支持，已重置为模型默认；尚未保存。'
  }
}
let modelRequestId = 0
let pollTimer = null
let generation = 0
const authorizing = computed(() => ['pending', 'starting', 'waiting', 'awaiting_authorization', 'completed'].includes(login.value?.login_status || status.value.oauth?.login_status))
const loginDisabledReason = computed(() => {
  if (loading.value || checking.value) return '正在检查服务端授权状态…'
  if (loadError.value) return '配置读取失败，请重试；当前不能确认授权能力。'
  if (legacyBackend.value) return '后端接口未更新，授权入口暂不可用。'
  if (!statusReady.value) return '请先刷新配置状态。'
  if (status.value.oauth.connected) return '该服务已授权；保存配置后可测试已保存的接入。'
  if (oauthBusy.value || authorizing.value) return '授权进行中，请完成授权或取消后重试。'
  if (!status.value.oauth.available) return status.value.oauth.message || '服务端尚未具备授权能力，请检查 CLI 与配置。'
  return ''
})
const safeAuthUrl = computed(() => {
  try {
    const url = new URL(login.value?.auth_url)
    return url.protocol === 'https:' && ['auth.openai.com', 'auth0.openai.com', 'chatgpt.com'].includes(url.hostname) ? url.href : ''
  } catch { return '' }
})

async function refreshStatus() {
  const current = generation
  checking.value = true
  try {
    const result = await getAIStatus()
    if (current !== generation) return
    status.value = result && typeof result === 'object' ? result : {}
    statusLoaded.value = true
    emit('status', status.value)
    loadError.value = ''
    if (!backendCompatible.value) {
      login.value = null
      stopPolling()
      // Preserve readable legacy fields (e.g. enabled) without allowing writes.
      return status.value
    }
    if (result.oauth?.login_status && login.value) login.value.login_status = result.oauth.login_status
    if (result.oauth?.connected) login.value = null
    return result
  } catch (error) {
    if (current === generation) loadError.value = error.response?.status === 404
      ? BACKEND_UPGRADE_MESSAGE
      : '无法读取 AI 配置，请检查后端连接后重试；这不代表未安装 Codex CLI。'
  } finally { if (current === generation) checking.value = false }
}

function stopPolling() {
  clearTimeout(pollTimer)
  pollTimer = null
}
function schedulePoll() {
  stopPolling()
  if (!props.modelValue || !authorizing.value) return
  pollTimer = setTimeout(async () => {
    await refreshStatus()
    schedulePoll()
  }, 3000)
}

watch(() => props.modelValue, async open => {
  generation++
  stopPolling()
  form.api_key = ''
  if (!open) return
  loading.value = true
  statusLoaded.value = false
  loadError.value = ''
  feedback.value = null
  reasoningNotice.value = ''
  form.oauth_reasoning_effort = ''
  const result = await refreshStatus()
  if (result && props.modelValue) {
    for (const key of Object.keys(form)) {
      if (!['api_key', 'clear_api_key'].includes(key) && result[key] !== undefined) form[key] = result[key]
    }
    form.clear_api_key = false
    schedulePoll()
  }
  loading.value = false
})

async function loadModels() {
  if (!statusReady.value || !status.value.oauth?.connected || modelsLoading.value) return
  const current = generation
  const requestId = ++modelRequestId
  modelsLoading.value = true
  modelsError.value = ''
  try {
    const result = await getAIOAuthModels()
    if (current !== generation || requestId !== modelRequestId) return
    modelOptions.value = result.ok && Array.isArray(result.models)
      ? result.models.filter(model => typeof model.id === 'string' && model.id && typeof model.label === 'string') : []
    if (!result.ok) modelsError.value = result.message || '模型目录暂不可用，可手动输入模型 ID 或使用默认模型。'
    else if (!modelOptions.value.length) modelsError.value = 'Codex 未返回可选模型；可保留账号默认模型，或手动填写已确认可用的模型 ID。'
  } catch (error) {
    if (current !== generation || requestId !== modelRequestId) return
    modelOptions.value = []
    modelsError.value = error.response?.status === 404
      ? '模型目录接口尚未加载，需安排重启后端后刷新列表；目前可手动输入模型 ID 或保留默认。'
      : '模型目录读取失败，可重试、手动输入模型 ID 或保留默认；不会影响保存配置。'
  } finally {
    if (current === generation && requestId === modelRequestId) {
      modelsLoading.value = false
      modelsLoaded.value = true
    }
  }
}

watch([() => props.modelValue, () => form.auth_mode, () => status.value.oauth?.connected, () => statusReady.value], ([open, mode, connected]) => {
  if (!open || !connected) {
    modelRequestId++
    modelOptions.value = []
    modelsLoaded.value = false
    modelsLoading.value = false
    modelsError.value = ''
  } else if (mode === 'openai_oauth' && !modelsLoaded.value) {
    loadModels()
  }
})

async function retryLoad() {
  const result = await refreshStatus()
  if (!result) return
  for (const key of Object.keys(form)) {
    if (!['api_key', 'clear_api_key'].includes(key) && result[key] !== undefined) form[key] = result[key]
  }
}
async function save() {
  if (saving.value || !statusReady.value || checking.value || loading.value) return
  if (form.auth_mode === 'api_key' && (!form.base_url.trim() || !form.model.trim())) {
    feedback.value = { ok: false, message: '请完整填写 API 地址和模型。' }
    return
  }
  if ((form.oauth_model || '').trim().length > 120) {
    feedback.value = { ok: false, message: '账号模型 ID 不能超过 120 个字符。' }
    return
  }
  if (form.auth_mode === 'openai_oauth' && incompatibleEffort.value) {
    feedback.value = { ok: false, message: '配置未保存：请选择所选模型支持的思考强度或模型默认。' }
    return
  }
  saving.value = true
  try {
    const payload = { ...form, oauth_model: (form.oauth_model || '').trim() }
    if (form.auth_mode === 'openai_oauth') {
      // Keep the server's API settings, not hidden unsaved edits from another tab.
      for (const key of ['base_url', 'model', 'api_format', 'max_tokens', 'temperature']) payload[key] = status.value[key]
      payload.clear_api_key = false
      delete payload.api_key
    } else {
      // Switching to API Key must not commit hidden OAuth drafts.
      payload.oauth_model = status.value.oauth_model || ''
      payload.oauth_reasoning_effort = status.value.oauth_reasoning_effort || ''
      if (!payload.api_key) delete payload.api_key
    }
    // A pre-effort backend rejects unknown fields; default remains backwards compatible.
    if (status.value.oauth_reasoning_effort === undefined && !payload.oauth_reasoning_effort) delete payload.oauth_reasoning_effort
    const result = await saveAIConfig(payload)
    status.value = result
    emit('status', result)
    form.api_key = ''
    form.clear_api_key = false
    form.oauth_model = result.oauth_model || ''
    form.oauth_reasoning_effort = result.oauth_reasoning_effort || ''
    feedback.value = result.auth_mode === 'openai_oauth'
      ? { ok: !result.enabled || result.ready === true,
          message: '配置已保存：OpenAI 账号 · ' + (result.oauth_model || '账号默认模型') + ' · 思考强度：' + (result.oauth_reasoning_effort || '模型默认') + '。'
            + (!result.enabled ? 'AI 当前处于停用状态。' : result.ready ? '分析通道已就绪，可测试已保存配置。' : '但分析通道尚未就绪，这不代表保存失败；请处理上方授权或隔离兼容提示。') }
      : { ok: true, message: '配置已保存。新请求使用新配置，正在执行的请求继续使用原配置。' }
  } catch (error) {
    const reasons = { 403: '仅允许本机可信工作台保存，请检查访问地址。', 404: BACKEND_UPGRADE_MESSAGE, 422: '配置参数未通过校验，请检查模型 ID、API 地址及参数。', 503: '服务端配置目录无法写入，请检查目录权限。' }
    feedback.value = { ok: false, message: reasons[error.response?.status]
      ? '配置未保存：' + reasons[error.response.status] : '未收到保存成功确认，请刷新状态核对已保存配置。' }
  }
  finally { saving.value = false }
}
async function testConnection() {
  if (!statusReady.value || checking.value || loading.value) return
  testing.value = true
  try { feedback.value = await testAIConnection() }
  catch { feedback.value = { ok: false, message: '连接测试失败，请检查网络及服务端状态。' } }
  finally { testing.value = false }
}
async function startLogin() {
  if (loginDisabledReason.value) return
  oauthBusy.value = true
  feedback.value = null
  try {
    login.value = await startAIOAuth()
    if (!authorizing.value && !login.value?.connected) {
      feedback.value = { ok: false, message: login.value?.message || '授权未启动，请检查服务端 Codex CLI 与网络。' }
    }
    await refreshStatus()
    schedulePoll()
  } catch (error) {
    feedback.value = { ok: false, message: error.response?.status === 404
      ? BACKEND_UPGRADE_MESSAGE : '授权未启动，请检查服务端 Codex CLI 与网络。' }
  }
  finally { oauthBusy.value = false }
}
async function cancelLogin() {
  try { await cancelAIOAuth(); login.value = null; stopPolling(); await refreshStatus() }
  catch { feedback.value = { ok: false, message: '取消授权失败，请刷新状态。' } }
}
async function logout() {
  try { await logoutAIOAuth(); login.value = null; stopPolling(); await refreshStatus() }
  catch { feedback.value = { ok: false, message: '断开失败，请刷新状态。' } }
}
onBeforeUnmount(() => { generation++; stopPolling(); form.api_key = '' })
</script>

<style scoped>
.connection-panel { display: flex; flex-direction: column; gap: 16px; }
.connection-panel :deep(.el-form-item) { margin-top: 18px; margin-bottom: 0; }
.form-help { display: block; color: var(--claw-text-secondary); font-size: 12px; line-height: 1.6; margin-top: 6px; }
.parameter-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.parameter-grid :deep(.el-input-number) { width: 100%; }
.oauth-status, .oauth-actions, .drawer-footer { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 16px; }
.oauth-status { color: var(--claw-text-secondary); font-size: 13px; }
.oauth-actions :deep(.el-button) { margin-left: 0; }
.setup-note { background: var(--claw-bg-card); border: 1px solid var(--claw-border); border-radius: 8px; padding: 14px; line-height: 1.7; margin-top: 14px; color: var(--claw-text-secondary); overflow-wrap: anywhere; }
.setup-note p { margin: 6px 0; }
.setup-note a { color: var(--el-color-primary); }
.drawer-footer { justify-content: flex-end; margin-top: 0; }
.drawer-feedback { margin-bottom: 12px; text-align: left; }
</style>
