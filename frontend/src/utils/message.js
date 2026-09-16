let messageStylePromise = null
let messageApiPromise = null

async function loadMessageApi() {
  if (!messageStylePromise) {
    messageStylePromise = import('element-plus/es/components/message/style/css.mjs')
  }
  if (!messageApiPromise) {
    messageApiPromise = import('element-plus/es/components/message/index.mjs').then((mod) => mod.ElMessage)
  }
  const [, messageApi] = await Promise.all([messageStylePromise, messageApiPromise])
  return messageApi
}

function openMessage(type, message, options = {}) {
  void loadMessageApi().then((ElMessage) => {
    if (typeof message === 'string') {
      ElMessage({ type, message, ...options })
      return
    }
    ElMessage({ type, ...(message || {}), ...options })
  })
}

export function notifySuccess(message, options) {
  openMessage('success', message, options)
}

export function notifyWarning(message, options) {
  openMessage('warning', message, options)
}

export function notifyError(message, options) {
  openMessage('error', message, options)
}

export function notifyInfo(message, options) {
  openMessage('info', message, options)
}
