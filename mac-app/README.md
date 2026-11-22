# WarmPlayer macOS 壳（v0.81）

Swift + WKWebView 单文件壳，把「黑胶唱片机 + 磁带机」WebApp 包成一个双击即用的 .app。
不是真原生重写：页面/音频/扫描逻辑仍在包内 Python 服务里，壳负责窗口、菜单、媒体键、子进程生灭。

## 目录与产物
- `main.swift`：壳全部源码（窗口/菜单/设置/日志/媒体键/Now Playing/子进程管理）。
- `build.sh`：组装脚本。PyInstaller onedir 打 helper → swiftc 编壳 → 拷 ffmpeg/ffprobe/pages/icns → ad-hoc 签名。
- `icon.png`：图标源（1536²）。
- 产物：`../WarmPlayer.app`（约 28MB）。中间产物默认 `/tmp/wpbuild`（`WPBUILD` 可改）。

## 构建依赖
- macOS 12+，Xcode command line tools（swiftc）。
- `~/.warmplayer/buildvenv`：python3.9 + pyinstaller 6.22.3 + numpy 2.0.2（pip 清华源装）。
- ffmpeg/ffprobe：取 `which ffmpeg/ffprobe`（本机 /usr/local/bin，8.0.1），解引用拷入包。
- 页面源：`../唱片机-服务版/{唱片机.html,磁带机.html}`。

复跑：`bash mac-app/build.sh`。

## 包内布局
```
WarmPlayer.app/Contents/
  MacOS/WarmPlayer            # Swift 壳
  Resources/
    helper/warmserver         # PyInstaller onedir（含 numpy）
    ffmpeg  ffprobe           # 8.0.1 静态二进制
    pages/{唱片机.html,磁带机.html}
    AppIcon.icns
  Info.plist                  # com.warmplayer.mac / 0.81.0 / NSAllowsLocalNetworking
```

## 启动逻辑
1. 探活 8900–8939 `/api/health`（0.6s 超时）。有服务（launchd 版/其它实例）→ **复用**，不启子进程。
   单实例 flock（`~/.warmplayer/server.lock`）决定两者不能并存，复用优先即规避冲突。
2. 无服务 → spawn `Resources/helper/warmserver`，env 注入：
   `WARMPLAYER_DATA`=数据根、`WARMPLAYER_PAGES`=包内 pages、`WARMPLAYER_FFMPEG/FFPROBE`=包内二进制、`--port`(设置值,0=自动)。
   子进程崩溃重试≤3；app 退出 terminate + 3s 后 SIGKILL，随 app 生灭，无独立后台程序。
3. 端口从子进程 stdout 行「黑胶唱片机 → http://127.0.0.1:PORT/」解析；WKWebView 加载该地址。

## 数据根
默认 `~/Library/Application Support/WarmPlayer/`（cache / settings.json / folders.json）。
本机已把原脚本目录三项**移动**至此并留符号链接指回，launchd 版与 app 共享同一物理缓存，免重扫。
设置窗可改数据根（改后重启服务生效）。

## 菜单
| 菜单 | 键 | 行为 |
|---|---|---|
| 设置 | ⌘, | 端口/主机/数据目录（UserDefaults: port/host/dataDir） |
| 日志 | ⌘L | 独立窗口 tail `~/Library/Logs/WarmPlayer.log` |
| 开机自启 | — | SMAppService.mainApp（macOS 13+） |
| 播放/暂停 | 空格 | 页内 __vpPlay/__vpPause 或 __tpPlay/__tpPause |
| 上一首/下一首 | — | __vpJump/__tpJump |
| 切换唱片↔磁带 | ⌘T | 页内 #modeBtn.click()（共享 session 续播） |
| 刷新 | ⌘R | webView.reload |

媒体键：MPRemoteCommandCenter play/pause/next/prev；Now Playing 2s 轮询 #nowTitle/#nowSub + __vpSession/__tpSession。

## 签名与分发
ad-hoc 签名（`codesign -s -`），本机双击可用。对外分发需 Apple Developer 账号做 Developer ID 签名 + 公证（notarization），未做。

## 已知边界
- 开机自启需 macOS 13+；12.x 该菜单项置灰。
- 复用模式下「日志」窗口看的是壳日志；服务日志在 launchd 的 /tmp/vinyl_srv.log。
- 设置改端口/数据根后需重启 app 内服务（壳会 terminate 旧子进程再 spawn）。