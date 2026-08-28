#!/bin/bash
# 重新生成「联动桌宠.app」访达启动器（macOS）
# 用法: bash scripts/make-app.sh [输出目录]   默认在当前仓库根目录生成
# 说明: .app 已加入 .gitignore，克隆仓库后跑一次本脚本即可得到访达/Dock 图标版启动器
set -e
cd "$(dirname "$0")/.."

OUT_DIR="${1:-.}"
APP="$OUT_DIR/联动桌宠.app"
REPO="$(pwd)"

# 1. 鲸鱼 Dock 图标（源码运行时的应用图标）: 没有就从原 .app 取，再没有就跳过
if [ ! -f "$REPO/assets/icon.icns" ]; then
  SRC="/Applications/dsh-pet-standalone-webm-chat.app/Contents/Resources/icon.icns"
  if [ -f "$SRC" ]; then cp "$SRC" "$REPO/assets/icon.icns"; fi
fi

# 2. 搭建 App 结构
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
if [ -f "$REPO/assets/icon.icns" ]; then
  cp "$REPO/assets/icon.icns" "$APP/Contents/Resources/icon.icns"
fi

# 3. Info.plist
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleName</key>
	<string>联动桌宠</string>
	<key>CFBundleDisplayName</key>
	<string>联动桌宠</string>
	<key>CFBundleIdentifier</key>
	<string>local.dsh-pet.linked</string>
	<key>CFBundleExecutable</key>
	<string>launcher</string>
	<key>CFBundleIconFile</key>
	<string>icon</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleShortVersionString</key>
	<string>1.0</string>
	<key>CFBundleVersion</key>
	<string>1</string>
	<key>LSMinimumSystemVersion</key>
	<string>11.0</string>
	<key>LSApplicationCategoryType</key>
	<string>public.app-category.utilities</string>
	<key>LSUIElement</key>
	<true/>
	<key>NSHighResolutionCapable</key>
	<true/>
</dict>
</plist>
PLIST

# 4. 启动器（arch -arm64 强制 arm64；以信标端口判断是否已在运行）
cat > "$APP/Contents/MacOS/launcher" <<'SH'
#!/bin/bash
REPO="__REPO__"
cd "$REPO" || exit 1
notify() { osascript -e "display notification \"$1\" with title \"dsh-pet\"" 2>/dev/null; }
if curl -s --max-time 1 http://127.0.0.1:47890/health >/dev/null 2>&1; then
  notify "联动桌宠已经在运行啦 🐳"; exit 0
fi
/usr/bin/arch -arm64 ./.venv/bin/python - "$REPO" <<'PYEOF'
import subprocess, sys, os
cwd = sys.argv[1]
os.makedirs(os.path.join(cwd, ".run"), exist_ok=True)
log = open(os.path.join(cwd, ".run", "pet.log"), "ab")
p = subprocess.Popen(
    ["/usr/bin/arch", "-arm64", os.path.join(cwd, ".venv", "bin", "python"), "-m", "pet"],
    cwd=cwd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    start_new_session=True,
)
open(os.path.join(cwd, ".run", "pet.pid"), "w").write(str(p.pid))
PYEOF
sleep 6
if curl -s --max-time 1 http://127.0.0.1:47890/health >/dev/null 2>&1; then
  notify "联动桌宠已启动 🐳"
else
  notify "联动桌宠启动失败，请看 .run/pet.log"; exit 1
fi
exit 0
SH
# 把仓库路径写进启动器
python3 - "$APP/Contents/MacOS/launcher" "$REPO" <<'PYEOF'
import sys
path, repo = sys.argv[1], sys.argv[2]
s = open(path, encoding="utf-8").read()
open(path, "w", encoding="utf-8").write(s.replace("__REPO__", repo))
PYEOF
chmod +x "$APP/Contents/MacOS/launcher"

# 5. ad-hoc 签名（避免 Gatekeeper 拦截）
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || true

echo "[done] 已生成 $APP"
echo "可拖入 /Applications 或 访达/Dock 使用。"
