#!/bin/bash
# 黑胶唱片机 · 本地服务
cd "$(dirname "$0")" || exit 1

PY=""
# 优先用带 numpy 的解释器（频谱在服务端算，需要 numpy）；没有再退回系统 python3
for c in /usr/local/bin/python3 /opt/homebrew/bin/python3 python3 /usr/bin/python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -z "$PY" ]; then
  echo "没找到 python3。请先安装 Python 3，或按「使用说明.md」手动运行。"
  echo "按回车关闭窗口。"; read -r _; exit 1
fi

# 1) 先停掉上一份服务（窗口关了但进程还挂着的孤儿进程）
OLD=$(lsof -nP -iTCP:8900 -sTCP:LISTEN -t 2>/dev/null | head -1)
if [ -n "$OLD" ]; then
  echo "发现上一份还在运行（PID $OLD），先停掉…"
  kill -9 "$OLD" 2>/dev/null
  sleep 2
fi

# 2) 关掉之前遗留的「启动唱片机」终端窗口（保留当前这个），避免越开越多
CUR_TTY=$(tty 2>/dev/null)
if [ -n "$CUR_TTY" ]; then
osascript <<EOF >/dev/null 2>&1
tell application "Terminal"
  repeat with w in (every window)
    try
      if ((name of w) contains "启动唱片机") and ((tty of w) is not "$CUR_TTY") then close w saving no
    end try
  end repeat
end tell
EOF
fi

clear
echo "──────────────────────────────────────────"
echo "  黑胶唱片机  正在启动…"
echo "  日志在下面实时滚动：扫描 / 添加 / 杜比下混都会打印。"
echo "  关闭这个窗口即停止服务。"
echo "──────────────────────────────────────────"
echo
"$PY" -u vinyl_server.py
EXIT=$?
echo
echo "服务已退出（代码 $EXIT）。按回车关闭窗口。"
read -r _
