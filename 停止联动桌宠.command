#!/bin/bash
# 停止「联动桌宠」—— 按信标端口 47890 精确定位并结束
osascript -e 'display notification "正在停止联动桌宠…" with title "dsh-pet"' 2>/dev/null
PID=$(lsof -tiTCP:47890 -sTCP:LISTEN 2>/dev/null)
if [ -n "$PID" ]; then
  kill $PID 2>/dev/null
  sleep 2
fi
# 兜底：结束 python -m pet 进程
pkill -f "Python -m pet" 2>/dev/null
pkill -f "bin/python -m pet" 2>/dev/null
sleep 1
if lsof -tiTCP:47890 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "仍有进程存活，请用活动监视器强制退出"
  exit 1
else
  echo "联动桌宠已停止"
fi
