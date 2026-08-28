#!/bin/bash
# 启动「联动桌宠」—— 带 DSH 工作状态联动的源码版 dsh-pet
# 双击运行即可；已在运行时不做任何事。
cd "$(dirname "$0")" || exit 1

# 已在运行判定：信标端口 47890（联动桌宠独有信号，避免进程名误匹配）
if curl -s --max-time 1 http://127.0.0.1:47890/health >/dev/null 2>&1; then
  osascript -e 'display notification "联动桌宠已经在运行啦 🐳" with title "dsh-pet"' 2>/dev/null
  exit 0
fi

mkdir -p .run

# 用 Python start_new_session 彻底脱离当前会话；arch -arm64 强制 arm64
/usr/bin/arch -arm64 ./.venv/bin/python - "$PWD" <<'PYEOF'
import subprocess, sys, os
cwd = sys.argv[1]
log = open(os.path.join(cwd, ".run", "pet.log"), "ab")
p = subprocess.Popen(
    ["/usr/bin/arch", "-arm64", os.path.join(cwd, ".venv", "bin", "python"), "-m", "pet"],
    cwd=cwd,
    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    start_new_session=True,
)
open(os.path.join(cwd, ".run", "pet.pid"), "w").write(str(p.pid))
PYEOF

sleep 6

if curl -s --max-time 1 http://127.0.0.1:47890/health >/dev/null 2>&1; then
  osascript -e 'display notification "联动桌宠已启动（信标监听 127.0.0.1:47890）" with title "dsh-pet"' 2>/dev/null
  echo "联动桌宠已启动（日志：.run/pet.log，PID：$(cat .run/pet.pid 2>/dev/null)）"
else
  osascript -e 'display notification "联动桌宠启动失败，请看 .run/pet.log" with title "dsh-pet"' 2>/dev/null
  echo "启动失败，日志："
  tail -20 .run/pet.log
  exit 1
fi
