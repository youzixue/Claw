import { ref, onMounted, onUnmounted } from 'vue'

let ws = null
let reconnectTimer = null
const listeners = new Map()

export function useWebSocket() {
  const connected = ref(false)
  const lastMessage = ref(null)

  function connect() {
    if (ws && ws.readyState === WebSocket.OPEN) return

    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const url = `${protocol}//${location.host}/api/v1/ws`

    ws = new WebSocket(url)

    ws.onopen = () => {
      connected.value = true
      console.log('🦅 WebSocket 已连接')
      if (reconnectTimer) {
        clearInterval(reconnectTimer)
        reconnectTimer = null
      }
      // 心跳
      reconnectTimer = setInterval(() => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'ping' }))
        }
      }, 30000)
    }

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        if (data.type === 'pong') return
        lastMessage.value = data
        // 分发到频道监听器
        const channel = data.channel
        if (channel && listeners.has(channel)) {
          listeners.get(channel).forEach((cb) => cb(data))
        }
      } catch (e) {
        // ignore
      }
    }

    ws.onclose = () => {
      connected.value = false
      console.log('WebSocket 已断开，5秒后重连...')
      setTimeout(connect, 5000)
    }

    ws.onerror = () => {
      ws.close()
    }
  }

  function disconnect() {
    if (reconnectTimer) {
      clearInterval(reconnectTimer)
      reconnectTimer = null
    }
    if (ws) {
      ws.close()
      ws = null
    }
    connected.value = false
  }

  function subscribe(channel, callback) {
    if (!listeners.has(channel)) {
      listeners.set(channel, new Set())
    }
    listeners.get(channel).add(callback)
    return () => listeners.get(channel)?.delete(callback)
  }

  return { connected, lastMessage, connect, disconnect, subscribe }
}
