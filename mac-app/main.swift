// WarmPlayer mac 壳：WKWebView + 包内 Python 服务子进程（探活复用优先）
// 编译：swiftc -O main.swift -o WarmPlayer -framework Cocoa -framework WebKit -framework MediaPlayer -framework ServiceManagement
import Cocoa
import WebKit
import MediaPlayer
import ServiceManagement

let kBundleID = "com.warmplayer.mac"
let kLogPath = ("~/Library/Logs/WarmPlayer.log" as NSString).expandingTildeInPath
let kDefaultData = ("~/Library/Application Support/WarmPlayer" as NSString).expandingTildeInPath

func logLine(_ s: String) {
    let line = "[\(Date())] \(s)\n"
    if let fh = FileHandle(forWritingAtPath: kLogPath) { fh.seekToEndOfFile(); fh.write(line.data(using: .utf8)!); fh.closeFile() }
    else { try? line.data(using: .utf8)!.write(to: URL(fileURLWithPath: kLogPath)) }
    NotificationCenter.default.post(name: NSNotification.Name("WPLog"), object: nil)
}

final class ServerManager {
    var child: Process?
    var port: Int = 0
    var reused = false
    var retries = 0
    let defaults = UserDefaults.standard
    var configuredPort: Int { defaults.integer(forKey: "port") }
    var host: String { defaults.string(forKey: "host") ?? "127.0.0.1" }
    var dataDir: String { defaults.string(forKey: "dataDir") ?? kDefaultData }

    func health(_ p: Int) -> Bool {
        let url = URL(string: "http://127.0.0.1:\(p)/api/health")!
        var req = URLRequest(url: url); req.timeoutInterval = 0.6
        let sem = DispatchSemaphore(value: 0); var ok = false
        URLSession.shared.dataTask(with: req) { d, r, _ in
            if let d = d, let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any], j["ok"] as? Bool == true { ok = true }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 1.0)
        return ok
    }

    func probeLive() -> Int? {
        if configuredPort > 0 { return health(configuredPort) ? configuredPort : nil }
        for p in 8900...8939 where health(p) { return p }
        return nil
    }

    func start(completion: @escaping (Int?) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            if let live = self.probeLive() {
                self.reused = true; self.port = live
                logLine("复用已有服务 :\(live)")
                DispatchQueue.main.async { completion(live) }
                return
            }
            self.reused = false
            self.spawn(completion: completion)
        }
    }

    func spawn(completion: @escaping (Int?) -> Void) {
        let res = Bundle.main.resourcePath!
        let exe = res + "/helper/warmserver"
        guard FileManager.default.isExecutableFile(atPath: exe) else {
            logLine("缺少 helper：\(exe)"); DispatchQueue.main.async { completion(nil) }; return
        }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = ["--port", configuredPort > 0 ? String(configuredPort) : "0", "--host", host, "--no-open", "--data-dir", dataDir]
        var env = ProcessInfo.processInfo.environment
        env["WARMPLAYER_FFMPEG"] = res + "/ffmpeg"
        env["WARMPLAYER_FFPROBE"] = res + "/ffprobe"
        env["WARMPLAYER_PAGES"] = res + "/pages"
        env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe; p.standardError = pipe
        var buf = ""
        pipe.fileHandleForReading.readabilityHandler = { fh in
            let d = fh.availableData
            guard !d.isEmpty, let s = String(data: d, encoding: .utf8) else { return }
            buf += s
            while let r = buf.range(of: "\n") {
                let line = String(buf[..<r.lowerBound]); buf = String(buf[r.upperBound...])
                logLine("srv: " + line)
                if let m = line.range(of: "http://127.0.0.1:"), let end = line[m.upperBound...].firstIndex(of: "/") {
                    if let pt = Int(line[m.upperBound..<end]), self.port == 0 {
                        self.port = pt
                        DispatchQueue.main.async { completion(pt) }
                    }
                }
            }
        }
        p.terminationHandler = { proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            logLine("服务子进程退出 status=\(proc.terminationStatus)")
            if !AppDelegate.terminating && !self.reused && self.retries < 3 {
                self.retries += 1; self.port = 0
                DispatchQueue.main.asyncAfter(deadline: .now() + 2) { self.spawn(completion: completion) }
            }
        }
        do { try p.run(); child = p; logLine("拉起自带服务子进程 pid=\(p.processIdentifier)") }
        catch { logLine("拉起失败：\(error)"); DispatchQueue.main.async { completion(nil) } }
    }

    func stop() {
        guard let c = child, c.isRunning else { child = nil; return }
        c.terminate()
        let deadline = Date().addingTimeInterval(3)
        while c.isRunning && Date() < deadline { usleep(100_000) }
        if c.isRunning { kill(c.processIdentifier, SIGKILL) }
        child = nil
    }

    func restart(completion: @escaping (Int?) -> Void) {
        stop(); port = 0; retries = 0
        start(completion: completion)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate {
    static var terminating = false
    var window: NSWindow!
    var web: WKWebView!
    let server = ServerManager()
    var logWindow: NSWindow?
    var logView: NSTextView?
    var settingsWindow: NSWindow?
    var npTimer: Timer?
    var statusItem: NSMenuItem!

    func applicationDidFinishLaunching(_ n: Notification) {
        // 单实例
        let me = NSRunningApplication.current
        let others = NSRunningApplication.runningApplications(withBundleIdentifier: kBundleID).filter { $0 != me }
        if let o = others.first { o.activate(options: .activateAllWindows); NSApp.terminate(nil); return }
        try? FileManager.default.createDirectory(atPath: (kLogPath as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        if !FileManager.default.fileExists(atPath: kLogPath) { FileManager.default.createFile(atPath: kLogPath, contents: nil) }
        logLine("app 启动")

        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = false
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.allowsBackForwardNavigationGestures = false
        window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 800),
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.minSize = NSSize(width: 900, height: 600)
        window.title = "WarmPlayer"
        window.contentView = web
        window.center()
        window.makeKeyAndOrderFront(nil)
        buildMenu()
        setupRemoteCommands()

        server.start { [weak self] port in
            guard let self = self else { return }
            if let port = port {
                self.statusItem.title = self.server.reused ? "服务：复用已有 :\(port)" : "服务：自带子进程 :\(port)"
                self.web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!))
                self.startNowPlaying()
            } else {
                self.statusItem.title = "服务：启动失败（查看日志）"
                let a = NSAlert(); a.messageText = "WarmPlayer 服务启动失败"; a.informativeText = "请查看日志（⌘L）排查。"
                a.runModal()
            }
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ s: NSApplication) -> Bool { true }
    func applicationWillTerminate(_ n: Notification) {
        AppDelegate.terminating = true
        server.stop()
        logLine("app 退出")
    }

    // MARK: 菜单
    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem(); main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于 WarmPlayer", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        appMenu.addItem(.separator())
        statusItem = NSMenuItem(title: "服务：启动中…", action: nil, keyEquivalent: "")
        statusItem.isEnabled = false
        appMenu.addItem(statusItem)
        appMenu.addItem(withTitle: "设置…", action: #selector(openSettings), keyEquivalent: ",")
        appMenu.addItem(withTitle: "查看日志", action: #selector(openLog), keyEquivalent: "l")
        appMenu.addItem(.separator())
        let login = NSMenuItem(title: "开机自启", action: #selector(toggleLogin(_:)), keyEquivalent: "")
        if #available(macOS 13.0, *) { login.state = SMAppService.mainApp.status == .enabled ? .on : .off }
        appMenu.addItem(login)
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出 WarmPlayer", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let playItem = NSMenuItem(); main.addItem(playItem)
        let playMenu = NSMenu(title: "播放")
        playMenu.addItem(withTitle: "播放 / 暂停", action: #selector(cmdTogglePlay), keyEquivalent: " ")
        playMenu.addItem(withTitle: "下一曲", action: #selector(cmdNext), keyEquivalent: "")
        playMenu.addItem(withTitle: "上一曲", action: #selector(cmdPrev), keyEquivalent: "")
        playMenu.addItem(.separator())
        playMenu.addItem(withTitle: "切换唱片 / 磁带", action: #selector(cmdMode), keyEquivalent: "t")
        playItem.submenu = playMenu

        let viewItem = NSMenuItem(); main.addItem(viewItem)
        let viewMenu = NSMenu(title: "视图")
        viewMenu.addItem(withTitle: "刷新页面", action: #selector(cmdReload), keyEquivalent: "r")
        viewItem.submenu = viewMenu

        let winItem = NSMenuItem(); main.addItem(winItem)
        let winMenu = NSMenu(title: "窗口")
        winMenu.addItem(withTitle: "最小化", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        winMenu.addItem(withTitle: "缩放", action: #selector(NSWindow.performZoom(_:)), keyEquivalent: "")
        winItem.submenu = winMenu
        NSApp.windowsMenu = winMenu
        NSApp.mainMenu = main
    }

    func evalJS(_ js: String, _ done: ((Any?) -> Void)? = nil) {
        web.evaluateJavaScript(js) { r, _ in done?(r) }
    }
    @objc func cmdTogglePlay() { evalJS("(function(){var s=window.__tpSession?__tpSession():(window.__vpSession?__vpSession():null);if(!s)return 'none';if(s.playing){(window.__tpPause||window.__vpPause)();}else{(window.__tpPlay||window.__vpPlay)();}return 'ok';})()") }
    @objc func cmdNext() { evalJS("(function(){var s=window.__tpSession?__tpSession():(window.__vpSession?__vpSession():null);if(!s)return 'none';(window.__tpJump||window.__vpJump)(s.track+1);return 'ok';})()") }
    @objc func cmdPrev() { evalJS("(function(){var s=window.__tpSession?__tpSession():(window.__vpSession?__vpSession():null);if(!s)return 'none';(window.__tpJump||window.__vpJump)(s.ct>3?s.track:s.track-1);return 'ok';})()") }
    @objc func cmdMode() { evalJS("(function(){var b=document.getElementById('modeBtn');if(b){b.click();return 'ok'}return 'none'})()") }
    @objc func cmdReload() { web.reload() }

    @objc func toggleLogin(_ sender: NSMenuItem) {
        if #available(macOS 13.0, *) {
            do {
                if SMAppService.mainApp.status == .enabled { try SMAppService.mainApp.unregister(); sender.state = .off }
                else { try SMAppService.mainApp.register(); sender.state = .on }
            } catch { logLine("开机自启设置失败：\(error)") }
        }
    }

    // MARK: 媒体键 / Now Playing
    func setupRemoteCommands() {
        let c = MPRemoteCommandCenter.shared()
        c.playCommand.addTarget { [weak self] _ in self?.evalJS("(function(){var s=window.__tpSession?__tpSession():(window.__vpSession?__vpSession():null);if(s&&!s.playing)(window.__tpPlay||window.__vpPlay)();return 'ok';})()"); return .success }
        c.pauseCommand.addTarget { [weak self] _ in self?.evalJS("(function(){var s=window.__tpSession?__tpSession():(window.__vpSession?__vpSession():null);if(s&&s.playing)(window.__tpPause||window.__vpPause)();return 'ok';})()"); return .success }
        c.togglePlayPauseCommand.addTarget { [weak self] _ in self?.cmdTogglePlay(); return .success }
        c.nextTrackCommand.addTarget { [weak self] _ in self?.cmdNext(); return .success }
        c.previousTrackCommand.addTarget { [weak self] _ in self?.cmdPrev(); return .success }
    }
    func startNowPlaying() {
        npTimer?.invalidate()
        npTimer = Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.evalJS("(function(){var t=document.getElementById('nowTitle'),s=document.getElementById('nowSub');var sess=window.__tpSession?__tpSession():(window.__vpSession?__vpSession():null);return JSON.stringify({title:t?t.textContent:'',sub:s?s.textContent:'',sess:sess});})()") { r in
                guard let str = r as? String, let d = str.data(using: .utf8),
                      let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { return }
                var info: [String: Any] = [:]
                info[MPMediaItemPropertyTitle] = j["title"] as? String ?? "WarmPlayer"
                info[MPMediaItemPropertyArtist] = j["sub"] as? String ?? ""
                if let sess = j["sess"] as? [String: Any] {
                    info[MPNowPlayingInfoPropertyElapsedPlaybackTime] = sess["ct"] as? Double ?? 0
                    info[MPMediaItemPropertyPlaybackDuration] = 0
                    info[MPNowPlayingInfoPropertyPlaybackRate] = (sess["playing"] as? Bool == true) ? 1.0 : 0.0
                } else {
                    info[MPNowPlayingInfoPropertyPlaybackRate] = 0.0
                }
                MPNowPlayingInfoCenter.default().nowPlayingInfo = info
            }
        }
    }

    // MARK: 日志窗
    @objc func openLog() {
        if logWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 480),
                             styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
            w.title = "WarmPlayer 日志"
            let sv = NSScrollView(frame: w.contentView!.bounds)
            sv.autoresizingMask = [.width, .height]
            sv.hasVerticalScroller = true
            let tv = NSTextView(frame: sv.bounds)
            tv.autoresizingMask = [.width]
            tv.isEditable = false
            tv.isVerticallyResizable = true
            tv.textContainer?.widthTracksTextView = true
            tv.font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
            sv.documentView = tv
            w.contentView = sv
            logWindow = w; logView = tv
            NotificationCenter.default.addObserver(forName: NSNotification.Name("WPLog"), object: nil, queue: .main) { [weak self] _ in
                self?.refreshLog()
            }
        }
        refreshLog()
        logWindow?.center(); logWindow?.makeKeyAndOrderFront(nil)
    }
    func refreshLog() {
        guard let tv = logView, let s = try? String(contentsOfFile: kLogPath, encoding: .utf8) else { return }
        let lines = s.split(separator: "\n", omittingEmptySubsequences: false)
        tv.string = lines.suffix(2000).joined(separator: "\n")
        tv.scrollToEndOfDocument(nil)
    }

    // MARK: 设置窗
    @objc func openSettings() {
        if settingsWindow == nil {
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 240),
                             styleMask: [.titled, .closable], backing: .buffered, defer: false)
            w.title = "WarmPlayer 设置"
            let stack = NSStackView()
            stack.orientation = .vertical; stack.spacing = 12
            stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 20, right: 20)
            stack.translatesAutoresizingMaskIntoConstraints = false
            func row(_ label: String, _ key: String, _ fallback: String) -> NSTextField {
                let l = NSTextField(labelWithString: label)
                let f = NSTextField(string: UserDefaults.standard.string(forKey: key) ?? fallback)
                f.identifier = NSUserInterfaceItemIdentifier(key)
                f.widthAnchor.constraint(equalToConstant: 360).isActive = true
                let r = NSStackView(views: [l, f]); r.spacing = 8
                stack.addArrangedSubview(r)
                return f
            }
            let portF = row("端口（0=自动）", "port", "0")
            let hostF = row("监听地址", "host", "127.0.0.1")
            let dataF = row("数据目录", "dataDir", kDefaultData)
            let browse = NSButton(title: "选择…", target: self, action: #selector(pickDataDir(_:)))
            let dataRow = NSStackView(views: [dataF, browse]); dataRow.spacing = 8
            stack.addArrangedSubview(dataRow)
            let note = NSTextField(wrappingLabelWithString: "端口/监听/数据目录在 app 自带服务时生效；若复用已有服务（launchd/NAS 探活），请先退出该服务再重启 app。")
            note.font = NSFont.systemFont(ofSize: 11); note.textColor = .secondaryLabelColor
            stack.addArrangedSubview(note)
            let save = NSButton(title: "保存并重启服务", target: self, action: #selector(saveSettings(_:)))
            save.bezelStyle = .rounded; save.keyEquivalent = "\r"
            let cancel = NSButton(title: "取消", target: self, action: #selector(closeSettings))
            cancel.bezelStyle = .rounded
            let btns = NSStackView(views: [save, cancel]); btns.spacing = 12
            stack.addArrangedSubview(btns)
            w.contentView = stack
            NSLayoutConstraint.activate([
                stack.leadingAnchor.constraint(equalTo: w.contentView!.leadingAnchor),
                stack.trailingAnchor.constraint(equalTo: w.contentView!.trailingAnchor),
                stack.topAnchor.constraint(equalTo: w.contentView!.topAnchor),
            ])
            settingsWindow = w
            objc_setAssociatedObject(w, &kPortKey, portF, .OBJC_ASSOCIATION_RETAIN)
            objc_setAssociatedObject(w, &kHostKey, hostF, .OBJC_ASSOCIATION_RETAIN)
            objc_setAssociatedObject(w, &kDataKey, dataF, .OBJC_ASSOCIATION_RETAIN)
        }
        settingsWindow?.center(); settingsWindow?.makeKeyAndOrderFront(nil)
    }
    @objc func pickDataDir(_ sender: NSButton) {
        let p = NSOpenPanel(); p.canChooseDirectories = true; p.canChooseFiles = false; p.allowsMultipleSelection = false
        if p.runModal() == .OK, let u = p.url,
           let f = objc_getAssociatedObject(settingsWindow!, &kDataKey) as? NSTextField { f.stringValue = u.path }
    }
    @objc func saveSettings(_ sender: NSButton) {
        guard let w = settingsWindow,
              let pf = objc_getAssociatedObject(w, &kPortKey) as? NSTextField,
              let hf = objc_getAssociatedObject(w, &kHostKey) as? NSTextField,
              let df = objc_getAssociatedObject(w, &kDataKey) as? NSTextField else { return }
        let d = UserDefaults.standard
        d.set(Int(pf.stringValue) ?? 0, forKey: "port")
        d.set(hf.stringValue.isEmpty ? "127.0.0.1" : hf.stringValue, forKey: "host")
        d.set(df.stringValue.isEmpty ? kDefaultData : df.stringValue, forKey: "dataDir")
        w.close()
        if server.reused {
            let a = NSAlert(); a.messageText = "当前复用已有服务"
            a.informativeText = "设置已保存。退出其它 WarmPlayer 服务（launchd 等）后重启 app 才会用新设置拉起自带服务。"
            a.runModal()
            return
        }
        statusItem.title = "服务：重启中…"
        server.restart { [weak self] port in
            guard let self = self else { return }
            if let port = port {
                self.statusItem.title = "服务：自带子进程 :\(port)"
                self.web.load(URLRequest(url: URL(string: "http://127.0.0.1:\(port)/")!))
            } else { self.statusItem.title = "服务：启动失败（查看日志）" }
        }
    }
    @objc func closeSettings() { settingsWindow?.close() }

    func webView(_ w: WKWebView, didFail n: WKNavigation!, withError e: Error) { logLine("页面加载失败：\(e.localizedDescription)") }
}

var kPortKey: UInt8 = 0, kHostKey: UInt8 = 0, kDataKey: UInt8 = 0

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()