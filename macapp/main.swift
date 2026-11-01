// 看书宝 macOS 外壳：负责启动本地服务、承载网页界面、并作为摄像头权限的责任进程。
import Cocoa
import WebKit
import AVFoundation

let PROJECT = Bundle.main.bundleURL.deletingLastPathComponent().path
let PORT = 8770

final class AppDelegate: NSObject, NSApplicationDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var server: Process?
    var startedHere = false
    var misses = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        NSApp.setActivationPolicy(.regular)
        buildMenu()
        requestMediaAccess()

        let cfg = WKWebViewConfiguration()
        web = WKWebView(frame: NSRect(x: 0, y: 0, width: 1300, height: 880), configuration: cfg)
        web.setValue(false, forKey: "drawsBackground")

        window = NSWindow(contentRect: web.frame,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "看书宝"
        window.contentView = web
        window.center()
        window.setFrameAutosaveName("KanShuBaoMain")
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        boot()
        // 看门狗：服务挂了自动拉起来
        Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { _ in
            if self.ping() { self.misses = 0; return }
            self.misses += 1
            if self.misses >= 2 { self.misses = 0; self.restartServer() }
        }
    }

    func restartServer() {
        if let p = server, p.isRunning { p.terminate() }
        server = nil
        spawnServer()
    }

    // 由 App 本体申请，授权后子进程（python/ffmpeg）才能拿到摄像头
    func requestMediaAccess() {
        AVCaptureDevice.requestAccess(for: .video) { granted in
            if !granted {
                DispatchQueue.main.async { self.showDeniedHint() }
            }
        }
        AVCaptureDevice.requestAccess(for: .audio) { _ in }
    }

    func showDeniedHint() {
        let a = NSAlert()
        a.messageText = "摄像头权限未开启"
        a.informativeText = "看书宝需要摄像头才能拍作业。点「打开设置」后在 隐私与安全性 → 摄像头 里勾选看书宝，然后重启应用。"
        a.addButton(withTitle: "打开设置")
        a.addButton(withTitle: "稍后")
        if a.runModal() == .alertFirstButtonReturn {
            NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera")!)
        }
    }

    func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        main.addItem(appItem)
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "关于看书宝", action: #selector(info), keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "退出", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        appItem.submenu = appMenu

        let viewItem = NSMenuItem()
        main.addItem(viewItem)
        let viewMenu = NSMenu(title: "视图")
        viewMenu.addItem(withTitle: "刷新", action: #selector(reload), keyEquivalent: "r")
        viewMenu.addItem(withTitle: "摄像头权限设置…", action: #selector(openPrivacy), keyEquivalent: "")
        viewItem.submenu = viewMenu
        NSApp.mainMenu = main
    }

    @objc func openPrivacy() {
        NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera")!)
    }

    @objc func info() {
        let a = NSAlert()
        a.messageText = "看书宝 Mac 版"
        a.informativeText = "AI 作业批改与答疑。实时通话会向豆包语音服务传输麦克风音频；看题与批改会向视觉服务上传单帧图片。文字记忆保存在本机。"
        a.runModal()
    }

    @objc func reload() { web.reload() }

    // 已有服务就直接连；没有就自己拉起来
    func boot() {
        if ping() { loadUI(); return }
        spawnServer()
        loadUI()
    }

    func spawnServer() {
        let p = Process()
        let python = ["/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3"].first {
            FileManager.default.isExecutableFile(atPath: $0)
        } ?? "/usr/bin/python3"
        p.executableURL = URL(fileURLWithPath: python)
        p.arguments = ["-u", PROJECT + "/app/server.py"]
        p.currentDirectoryURL = URL(fileURLWithPath: PROJECT)
        let log = PROJECT + "/logs/server.log"
        try? FileManager.default.createDirectory(atPath: PROJECT + "/logs", withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: log, contents: nil)
        if let h = FileHandle(forWritingAtPath: log) { p.standardOutput = h; p.standardError = h }
        do { try p.run(); server = p; startedHere = true } catch { showError("启动失败：\(error)") }
    }

    func ping() -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(PORT)/api/status") else { return false }
        var ok = false
        let sem = DispatchSemaphore(value: 0)
        var req = URLRequest(url: url)
        req.timeoutInterval = 1.5
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            ok = (resp as? HTTPURLResponse)?.statusCode == 200
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 2.0)
        return ok
    }

    func loadUI() {
        let url = URL(string: "http://127.0.0.1:\(PORT)")!
        web.load(URLRequest(url: url))
        // 服务可能还在起，最多重试 20 次
        var tries = 0
        Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { t in
            tries += 1
            if self.ping() { self.web.load(URLRequest(url: url)); t.invalidate() }
            else if tries > 20 { t.invalidate(); self.showError("本地服务没有启动，看 logs/server.err.log") }
        }
    }

    func showError(_ msg: String) {
        let a = NSAlert()
        a.messageText = "看书宝"
        a.informativeText = msg
        a.runModal()
    }

    func applicationWillTerminate(_ note: Notification) {
        if startedHere { server?.terminate() }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
