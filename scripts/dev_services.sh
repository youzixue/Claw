#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="$ROOT_DIR/logs"
PID_DIR="$LOG_DIR/pids"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"
BACKEND_PORT="${BACKEND_PORT:-8000}"
FRONTEND_PORT="${FRONTEND_PORT:-5173}"
BACKEND_HOST="${BACKEND_HOST:-127.0.0.1}"
FRONTEND_HOST="${FRONTEND_HOST:-127.0.0.1}"
BACKEND_PID="$PID_DIR/backend.pid"
FRONTEND_PID="$PID_DIR/frontend.pid"
BACKEND_LOG="$LOG_DIR/backend-uvicorn.log"
FRONTEND_LOG="$LOG_DIR/frontend-vite.log"
LAUNCHD_DIR="$LOG_DIR/launchd"
BACKEND_LABEL="com.claw.dev.backend"
FRONTEND_LABEL="com.claw.dev.frontend"
BACKEND_PLIST="$LAUNCHD_DIR/$BACKEND_LABEL.plist"
FRONTEND_PLIST="$LAUNCHD_DIR/$FRONTEND_LABEL.plist"
LAUNCHD_DOMAIN="gui/$(id -u)"

mkdir -p "$LOG_DIR" "$PID_DIR" "$LAUNCHD_DIR"

usage() {
  cat <<USAGE
Usage: $0 {start|stop|restart|status|health|logs}

Environment:
  BACKEND_PORT   default 8000
  FRONTEND_PORT  default 5173
USAGE
}

pid_running() {
  local pid="${1:-}"
  [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1
}

pid_from_file() {
  local file="$1"
  [[ -f "$file" ]] && tr -d '[:space:]' < "$file" || true
}

port_pids() {
  local port="$1"
  lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true
}

use_launchd() {
  [[ "$(uname -s)" == "Darwin" ]] && command -v launchctl >/dev/null 2>&1
}

# 当脚本本身是被 launchd 调起时（登录自启路径），不能再嵌套执行
# launchctl bootstrap/bootout，否则会因 launchd domain 锁等待卡死。
# 此时只能 kickstart 已加载的 job，避免破坏守护关系。
in_launchd_context() {
  [[ "${LaunchAgent:-}" == "1" ]] || [[ -n "${XPC_SERVICE_NAME:-}" ]]
}

launchd_loaded() {
  local label="$1"
  launchctl print "$LAUNCHD_DOMAIN/$label" >/dev/null 2>&1
}

bootout_launchd() {
  local label="$1"
  if use_launchd && launchd_loaded "$label"; then
    echo "Stopping launchd service $label"
    launchctl bootout "$LAUNCHD_DOMAIN/$label" >/dev/null 2>&1 || true
    sleep 0.5
  fi
}

bootstrap_launchd() {
  local label="$1"
  local plist="$2"
  if launchctl bootstrap "$LAUNCHD_DOMAIN" "$plist"; then
    return 0
  fi
  echo "launchd bootstrap retry for $label"
  launchctl bootout "$LAUNCHD_DOMAIN/$label" >/dev/null 2>&1 || true
  sleep 1
  launchctl bootstrap "$LAUNCHD_DOMAIN" "$plist"
}

write_plist() {
  local label="$1"
  local plist="$2"
  local command="$3"
  local stdout="$4"
  local stderr="$5"
  cat > "$plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>-lc</string>
    <string>$command</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$stdout</string>
  <key>StandardErrorPath</key>
  <string>$stderr</string>
</dict>
</plist>
PLIST
}

stop_pid_file() {
  local name="$1"
  local file="$2"
  local pid
  pid="$(pid_from_file "$file")"
  if pid_running "$pid"; then
    echo "Stopping $name pid=$pid"
    kill "$pid" || true
    for _ in {1..30}; do
      pid_running "$pid" || break
      sleep 0.2
    done
    if pid_running "$pid"; then
      echo "Force stopping $name pid=$pid"
      kill -9 "$pid" || true
    fi
  fi
  rm -f "$file"
}

stop_port() {
  local port="$1"
  local pids
  pids="$(port_pids "$port")"
  [[ -z "$pids" ]] && return 0
  echo "Cleaning port $port pids: $pids"
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    kill "$pid" || true
  done <<< "$pids"
  sleep 0.5
  pids="$(port_pids "$port")"
  [[ -z "$pids" ]] && return 0
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    kill -9 "$pid" || true
  done <<< "$pids"
}

stop_all() {
  bootout_launchd "$FRONTEND_LABEL"
  bootout_launchd "$BACKEND_LABEL"
  stop_pid_file "frontend" "$FRONTEND_PID"
  stop_pid_file "backend" "$BACKEND_PID"
  stop_port "$FRONTEND_PORT"
  stop_port "$BACKEND_PORT"
}

start_backend() {
  local force="${1:-}"
  if [[ "$force" != "force" ]] && service_healthy "http://127.0.0.1:$BACKEND_PORT/health"; then
    echo "backend already healthy on $BACKEND_HOST:$BACKEND_PORT; skip restart"
    return 0
  fi
  if ! in_launchd_context; then
    stop_port "$BACKEND_PORT"
  fi
  echo "Starting backend on $BACKEND_HOST:$BACKEND_PORT"
  if use_launchd; then
    write_plist \
      "$BACKEND_LABEL" \
      "$BACKEND_PLIST" \
      "echo "'$$'" > '$BACKEND_PID'; cd '$BACKEND_DIR'; unset CLAW_DISABLE_SCHEDULER; exec python3 -m uvicorn app.main:app --host '$BACKEND_HOST' --port '$BACKEND_PORT'" \
      "$BACKEND_LOG" \
      "$BACKEND_LOG"
    if in_launchd_context && launchd_loaded "$BACKEND_LABEL"; then
      # 在 launchd 域内且 job 已加载：直接 kickstart 自拉起，
      # 不要再嵌套 bootout/bootstrap，否则会与 launchd 锁竞争卡死。
      launchctl kickstart -k "$LAUNCHD_DOMAIN/$BACKEND_LABEL" >/dev/null 2>&1 || true
    else
      bootout_launchd "$BACKEND_LABEL"
      bootstrap_launchd "$BACKEND_LABEL" "$BACKEND_PLIST"
      launchctl kickstart -k "$LAUNCHD_DOMAIN/$BACKEND_LABEL" >/dev/null 2>&1 || true
    fi
  else
    (
      cd "$BACKEND_DIR"
      unset CLAW_DISABLE_SCHEDULER
      nohup python3 -m uvicorn app.main:app --host "$BACKEND_HOST" --port "$BACKEND_PORT" \
        > "$BACKEND_LOG" 2>&1 &
      echo $! > "$BACKEND_PID"
    )
  fi
}

start_frontend() {
  local force="${1:-}"
  if [[ "$force" != "force" ]] && service_healthy "http://127.0.0.1:$FRONTEND_PORT/"; then
    echo "frontend already healthy on $FRONTEND_HOST:$FRONTEND_PORT; skip restart"
    return 0
  fi
  if ! in_launchd_context; then
    stop_port "$FRONTEND_PORT"
  fi
  echo "Starting frontend on $FRONTEND_HOST:$FRONTEND_PORT"
  if use_launchd; then
    write_plist \
      "$FRONTEND_LABEL" \
      "$FRONTEND_PLIST" \
      "echo "'$$'" > '$FRONTEND_PID'; cd '$FRONTEND_DIR'; exec npm run dev -- --host '$FRONTEND_HOST' --port '$FRONTEND_PORT'" \
      "$FRONTEND_LOG" \
      "$FRONTEND_LOG"
    if in_launchd_context && launchd_loaded "$FRONTEND_LABEL"; then
      launchctl kickstart -k "$LAUNCHD_DOMAIN/$FRONTEND_LABEL" >/dev/null 2>&1 || true
    else
      bootout_launchd "$FRONTEND_LABEL"
      bootstrap_launchd "$FRONTEND_LABEL" "$FRONTEND_PLIST"
      launchctl kickstart -k "$LAUNCHD_DOMAIN/$FRONTEND_LABEL" >/dev/null 2>&1 || true
    fi
  else
    (
      cd "$FRONTEND_DIR"
      nohup npm run dev -- --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" \
        > "$FRONTEND_LOG" 2>&1 &
      echo $! > "$FRONTEND_PID"
    )
  fi
}

wait_http() {
  local url="$1"
  local label="$2"
  for _ in {1..40}; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "$label healthy: $url"
      return 0
    fi
    sleep 0.5
  done
  echo "$label failed health check: $url" >&2
  return 1
}

# 一次性健康探测（不重试）。用于让 `start` 幂等：
# bootstrap 定时器会周期性调用 `start`，若无条件走 launchctl kickstart -k
# （-k = 先杀后起），会把交易时段内健康运行的后端反复杀死，
# 造成策略状态机中断（2026-09-16 复盘定位的生产事故）。
# `restart` 显式先 stop_all，因此不会被此探测跳过。
service_healthy() {
  curl -fsS --max-time 2 "$1" >/dev/null 2>&1
}

start_all() {
  local force="${1:-}"
  start_backend "$force"
  start_frontend "$force"
  wait_http "http://127.0.0.1:$BACKEND_PORT/health" "backend"
  wait_http "http://127.0.0.1:$FRONTEND_PORT/" "frontend"
  health
}

status() {
  local backend_pid frontend_pid
  backend_pid="$(pid_from_file "$BACKEND_PID")"
  frontend_pid="$(pid_from_file "$FRONTEND_PID")"
  echo "backend pid: ${backend_pid:-none} running: $(pid_running "$backend_pid" && echo yes || echo no)"
  echo "frontend pid: ${frontend_pid:-none} running: $(pid_running "$frontend_pid" && echo yes || echo no)"
  echo "backend port $BACKEND_PORT pids: $(port_pids "$BACKEND_PORT" | tr '\n' ' ')"
  echo "frontend port $FRONTEND_PORT pids: $(port_pids "$FRONTEND_PORT" | tr '\n' ' ')"
  if use_launchd; then
    echo "backend launchd loaded: $(launchd_loaded "$BACKEND_LABEL" && echo yes || echo no)"
    echo "frontend launchd loaded: $(launchd_loaded "$FRONTEND_LABEL" && echo yes || echo no)"
  fi
}

health() {
  echo "Backend health:"
  if ! curl -fsS --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health"; then
    echo "backend health failed" >&2
    return 1
  fi
  echo
  echo "Frontend health:"
  if ! curl -fsS --max-time 5 "http://127.0.0.1:$FRONTEND_PORT/" >/dev/null; then
    echo "frontend health failed" >&2
    return 1
  fi
  echo "ok"
}

show_logs() {
  echo "== backend log: $BACKEND_LOG =="
  tail -80 "$BACKEND_LOG" 2>/dev/null || true
  echo
  echo "== frontend log: $FRONTEND_LOG =="
  tail -80 "$FRONTEND_LOG" 2>/dev/null || true
}

case "${1:-}" in
  start)
    start_all
    ;;
  stop)
    stop_all
    ;;
  restart)
    stop_all
    start_all force
    ;;
  status)
    status
    ;;
  health)
    health
    ;;
  logs)
    show_logs
    ;;
  *)
    usage
    exit 2
    ;;
esac
