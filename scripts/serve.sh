#!/usr/bin/env bash
# 倪师 AI · 服务管理（本机开发用）
#
# 用法：
#   bash scripts/serve.sh start     启动（后台常驻，脱离当前终端）
#   bash scripts/serve.sh stop      停止
#   bash scripts/serve.sh restart   重启
#   bash scripts/serve.sh status    查看状态与访问地址
#   bash scripts/serve.sh logs      跟踪日志
#   bash scripts/serve.sh stop-orphan  结束占用端口的本项目残留进程
#
# 为什么需要它：直接用 `python3 src/nhx_app/server.py &` 启动的服务
# 会被绑在启动它的 shell 上，shell 一退（或被信号收走）服务就跟着被 SIGTERM。
# 这里用 setsid + nohup 让它脱离会话，并记录 PID 便于管理。
#
# 生产环境请用 systemd 或 Docker（见 deploy/）。

set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
REPO_ROOT="$(pwd)"

RUN_DIR="$REPO_ROOT/data"
PID_FILE="$RUN_DIR/server.pid"
LOG_FILE="$RUN_DIR/server.log"
PORT="${NHX_PORT:-}"

mkdir -p "$RUN_DIR"

# 从 .env 读端口（若环境变量没给）
if [ -z "$PORT" ] && [ -f "$REPO_ROOT/.env" ]; then
  PORT="$(grep -E '^\s*NHX_PORT=' "$REPO_ROOT/.env" | tail -1 | cut -d= -f2- | tr -d ' \r')"
fi
PORT="${PORT:-8790}"

_alive() {
  [ -f "$PID_FILE" ] || return 1
  local pid; pid="$(cat "$PID_FILE" 2>/dev/null)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null
}

_pid() { cat "$PID_FILE" 2>/dev/null; }

port_owner() {
  # 返回占用该端口的 PID（无则空）
  lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null | head -1
}

do_start() {
  if _alive; then
    printf '已在运行（PID %s）\n' "$(_pid)"
    do_status
    return 0
  fi

  # 端口可能被**别的**进程（孤儿进程、另一份服务）占着。
  # 必须先查——否则我们的进程会因「端口被占用」秒退，
  # 而健康检查却因为**别人的**服务有响应而误报成功。
  local owner; owner="$(port_owner)"
  if [ -n "$owner" ]; then
    printf '❌ 端口 %s 已被 PID %s 占用：\n' "$PORT" "$owner"
    ps -p "$owner" -o pid,command 2>/dev/null | tail -1 | sed 's/^/     /'
    printf '\n   若是本项目的残留进程： bash scripts/serve.sh stop-orphan\n'
    printf '   或换端口：在 .env 里改 NHX_PORT\n'
    return 1
  fi

  rm -f "$PID_FILE"

  # setsid + nohup：脱离当前会话，shell 退出也不影响
  if command -v setsid >/dev/null 2>&1; then
    setsid nohup python3 src/nhx_app/server.py >>"$LOG_FILE" 2>&1 &
  else
    # macOS 自带无 setsid，用 nohup + disown 达到同样效果
    nohup python3 src/nhx_app/server.py >>"$LOG_FILE" 2>&1 &
  fi
  local pid=$!
  echo "$pid" > "$PID_FILE"
  disown 2>/dev/null || true

  # 等端口就绪（最多 10 秒）。
  # 关键：不仅要端口通，还要确认**我们启动的这个 PID** 仍然活着——
  # 否则别的进程在服务时我们会误报成功。
  local i
  for i in $(seq 1 50); do
    if ! kill -0 "$pid" 2>/dev/null; then
      printf '❌ 启动失败，最后 20 行日志：\n\n'
      tail -20 "$LOG_FILE" | sed 's/^/  /'
      rm -f "$PID_FILE"
      return 1
    fi
    if curl -sf -m 2 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
      local real_owner; real_owner="$(port_owner)"
      if [ "$real_owner" = "$pid" ]; then
        printf '✅ 已启动（PID %s）\n\n' "$pid"
        do_status
        return 0
      fi
      printf '⚠️  端口 %s 上的服务是 PID %s，不是我们启动的 %s\n' \
             "$PORT" "$real_owner" "$pid"
      printf '   （可能是残留进程）\n'
      return 1
    fi
    sleep 0.2
  done

  printf '⚠️  进程在跑但端口 %s 未就绪，最后 20 行日志：\n\n' "$PORT"
  tail -20 "$LOG_FILE" | sed 's/^/  /'
  return 1
}

do_stop() {
  if ! _alive; then
    printf '（未在运行）\n'
    rm -f "$PID_FILE"
    return 0
  fi
  local pid; pid="$(_pid)"
  kill "$pid" 2>/dev/null
  local i
  for i in $(seq 1 30); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.2
  done
  if kill -0 "$pid" 2>/dev/null; then
    printf '优雅停止超时，强制结束\n'
    kill -9 "$pid" 2>/dev/null
  fi
  rm -f "$PID_FILE"
  printf '✅ 已停止\n'
}

do_status() {
  if _alive; then
    printf '状态：运行中（PID %s）\n' "$(_pid)"
  else
    printf '状态：未运行\n'
    printf '日志：%s\n' "$LOG_FILE"
    return 1
  fi

  printf '端口：%s\n\n' "$PORT"
  curl -s -m 5 "http://127.0.0.1:$PORT/api/health" 2>/dev/null \
    | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)['data']
except Exception:
    print('  （健康检查无响应）'); raise SystemExit(1)
print('  模型：%s' % d.get('model'))
print('  密钥：%s' % ('已配置' if d.get('llm_ready') else '❌ 未配置'))
print('  认证：%s' % d.get('auth_mode'))
" 2>/dev/null

  printf '\n访问地址（不要用 0.0.0.0）：\n'
  python3 - "$PORT" <<'PY' 2>/dev/null
import sys, os
sys.path.insert(0, 'src/nhx_app')
from server import browsable_urls
port = int(sys.argv[1])
for u in browsable_urls("0.0.0.0", port):
    print("  %s" % u)
PY
  printf '\n日志：%s\n' "$LOG_FILE"
}

do_stop_orphan() {
  local owner; owner="$(port_owner)"
  if [ -z "$owner" ]; then
    printf '端口 %s 未被占用\n' "$PORT"
    return 0
  fi
  printf '端口 %s 上的进程：\n' "$PORT"
  ps -p "$owner" -o pid,command 2>/dev/null | tail -1 | sed 's/^/  /'
  # 只杀本项目自己的服务，避免误伤别的程序
  if ps -p "$owner" -o command= 2>/dev/null | grep -q "nhx_app/server.py"; then
    kill "$owner" 2>/dev/null && printf '✅ 已结束（PID %s）\n' "$owner"
    sleep 1
  else
    printf '⚠️  该进程不是本项目的服务，未处理。请自行确认后再操作。\n'
    return 1
  fi
}

case "${1:-status}" in
  start)   do_start ;;
  stop-orphan) do_stop_orphan ;;
  stop)    do_stop ;;
  restart) do_stop; sleep 1; do_start ;;
  status)  do_status ;;
  logs)    tail -f "$LOG_FILE" ;;
  *)
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
