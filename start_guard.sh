#!/usr/bin/env bash
# ollama-loop-guard 启动脚本（Git Bash）
# 用法：start_guard.sh / stop_guard.sh
# stop 采用优雅退出：touch 停止标记 → guard 等活跃流跑完再退（最多 drain-timeout）→ 超时才强杀
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
# 停止标记用相对路径：cd 到 DIR 后 bash 与 Windows Python 的 CWD 指向同一文件
# （Git Bash 的 /g/... 路径 Windows Python 解析不了，必须用相对路径）
STOP_FILE=".guard-stop"

start() {
  if netstat -ano | grep -q ":11435.*LISTENING"; then
    echo "guard already running on :11435"
    return
  fi
  rm -f "$DIR/$STOP_FILE"
  cd "$DIR"
  python -u ollama_guard.py --port 11435 --log-file guard.log --stop-file "$STOP_FILE" --drain-timeout 60 >> guard.out 2>&1 &
  sleep 2
  if netstat -ano | grep -q ":11435.*LISTENING"; then
    echo "guard started (pid via netstat)"
  else
    echo "guard FAILED to start, check guard.out"
    return 1
  fi
}

stop() {
  if ! netstat -ano | grep -q ":11435.*LISTENING"; then
    echo "guard not running"
    return
  fi
  touch "$DIR/$STOP_FILE"
  # 等待优雅退出：最多 70s（guard drain 60s + 余量），期间轮询端口消失
  for i in $(seq 1 70); do
    if ! netstat -ano | grep -q ":11435.*LISTENING"; then
      echo "guard stopped gracefully"
      rm -f "$DIR/$STOP_FILE"
      return
    fi
    sleep 1
  done
  echo "guard did not drain within timeout, force-killing"
  for pid in $(netstat -ano | grep ":11435" | grep LISTENING | awk '{print $5}' | sort -u); do
    taskkill //F //PID "$pid" 2>/dev/null || true
  done
  rm -f "$DIR/$STOP_FILE"
  echo "guard stopped (forced)"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  *) echo "usage: $0 {start|stop|restart}"; exit 1 ;;
esac
