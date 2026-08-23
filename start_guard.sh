#!/usr/bin/env bash
# ollama-loop-guard 启动脚本（Git Bash）
# 用法：start_guard.sh / stop_guard.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

start() {
  if netstat -ano | grep -q ":11435.*LISTENING"; then
    echo "guard already running on :11435"
    return
  fi
  cd "$DIR"
  python -u ollama_guard.py --port 11435 --log-file guard.log >> guard.out 2>&1 &
  sleep 2
  if netstat -ano | grep -q ":11435.*LISTENING"; then
    echo "guard started (pid via netstat)"
  else
    echo "guard FAILED to start, check guard.out"
    return 1
  fi
}

stop() {
  for pid in $(netstat -ano | grep ":11435" | grep LISTENING | awk '{print $5}' | sort -u); do
    taskkill //F //PID "$pid" 2>/dev/null || true
  done
  echo "guard stopped"
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  *) echo "usage: $0 {start|stop|restart}"; exit 1 ;;
esac
