# Run WarmPlayer as a login service (macOS) / 常驻服务配置

`launchd` can start WarmPlayer at login and restart it automatically if it exits.
launchd 可以在登录时自动启动 WarmPlayer，并在进程退出后自动拉起。

## 1. Create the plist / 创建 plist

Save as `~/Library/LaunchAgents/com.warmplayer.server.plist`, and **replace the two paths** with your own checkout path and python path.
保存为 `~/Library/LaunchAgents/com.warmplayer.server.plist`，并把下面两处路径改成你自己的目录和 python 路径。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.warmplayer.server</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/python3</string>
        <string>-u</string>
        <string>vinyl_server.py</string>
        <string>--no-open</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/warmplayer</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>15</integer>
    <key>StandardOutPath</key>
    <string>/tmp/warmplayer.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/warmplayer.log</string>
</dict>
</plist>
```

## 2. Load it / 装载

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.warmplayer.server.plist
```

Check / 验证：

```bash
launchctl print gui/$(id -u)/com.warmplayer.server | head
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8900/
```

## 3. Remove it / 撤销

```bash
launchctl bootout gui/$(id -u)/com.warmplayer.server
rm ~/Library/LaunchAgents/com.warmplayer.server.plist
```

## Notes / 说明

- `KeepAlive` restarts the server ~15 s after it dies; the in-process watchdog (`health_loop`) additionally re-execs itself if it stops answering HTTP while still alive.
  `KeepAlive` 负责进程退出后的拉起；进程内的自检看门狗负责"活着但不应答"的原地重启，两层互补。
- The server never lets the machine sleep while it runs, but allows the display to sleep when nothing is playing.
  服务运行期间机器不会休眠；没有播放时显示器可以正常熄屏。
- Do not run two instances at once — they fight over the cache files and the port.
  不要同时跑两个实例，会抢缓存文件和端口。
