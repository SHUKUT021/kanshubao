// 摄像头物理按键监听器（作用域严格限定）。
//
// 安全边界（重要）：
//   1. 必须显式给一个设备名称关键字，只处理该设备的元素，其它设备全部丢弃；
//   2. 只接受 Button 页(0x09) 与 Consumer 页(0x0C)，键盘页(0x07) 永不订阅也不输出；
//   3. --list 只打印设备名称，不监听任何事件。
//
// 编译：xcrun swiftc -O tools/hidmon.swift -o tools/hidmon
// 用法：./tools/hidmon --list        列出 HID 设备名
//       ./tools/hidmon "USB Camera"   只监听该设备上的按键
import Foundation
import IOKit.hid

let args = Array(CommandLine.arguments.dropFirst())
let listMode = args.contains("--list")
let filter = args.first(where: { !$0.hasPrefix("--") })?.lowercased() ?? ""

if !listMode && filter.isEmpty {
    FileHandle.standardError.write("用法：hidmon --list | hidmon <设备名关键字>\n".data(using: .utf8)!)
    exit(2)
}

func jstr(_ s: String) -> String {
    let e = s.replacingOccurrences(of: "\\", with: "\\\\")
             .replacingOccurrences(of: "\"", with: "\\\"")
    return "\"\(e)\""
}

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj, options: []),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

let manager = IOHIDManagerCreate(kCFAllocatorDefault, IOOptionBits(kIOHIDOptionsTypeNone))
IOHIDManagerSetDeviceMatching(manager, nil)

func productName(_ device: IOHIDDevice) -> String {
    (IOHIDDeviceGetProperty(device, kIOHIDProductKey as CFString) as? String) ?? ""
}

let matchCallback: IOHIDDeviceCallback = { _, _, _, device in
    let name = IOHIDDeviceGetProperty(device, kIOHIDProductKey as CFString) as? String ?? "?"
    let vendor = IOHIDDeviceGetProperty(device, kIOHIDVendorIDKey as CFString) as? Int ?? 0
    let product = IOHIDDeviceGetProperty(device, kIOHIDProductIDKey as CFString) as? Int ?? 0
    let usagePage = IOHIDDeviceGetProperty(device, kIOHIDPrimaryUsagePageKey as CFString) as? Int ?? 0
    let usage = IOHIDDeviceGetProperty(device, kIOHIDPrimaryUsageKey as CFString) as? Int ?? 0
    if listMode {
        emit(["type": "device", "name": name, "vendor": vendor, "product": product,
              "usagePage": usagePage, "usage": usage])
        return
    }
    let lower = name.lowercased()
    if lower.contains(filter) {
        emit(["type": "device", "name": name, "vendor": vendor, "product": product,
              "usagePage": usagePage, "usage": usage, "matched": true])
    }
}

let inputCallback: IOHIDValueCallback = { _, _, _, value in
    if listMode || filter.isEmpty { return }
    let element = IOHIDValueGetElement(value)
    let v = IOHIDValueGetIntegerValue(value)
    if v == 0 { return }
    let page = IOHIDElementGetUsagePage(element)
    let usage = IOHIDElementGetUsage(element)
    let dev = IOHIDElementGetDevice(element)
    let name = productName(dev)
    // 双重保险：设备名不匹配就丢弃（包括所有键盘、触控板等）
    if !name.lowercased().contains(filter) { return }
    // 只认按键页 0x09 与消费者控制页 0x0C，键盘页 0x07 永远忽略
    guard page == 0x09 || page == 0x0C else { return }
    emit(["type": "button", "device": name, "page": page, "usage": usage, "value": v])
}

IOHIDManagerRegisterDeviceMatchingCallback(manager, matchCallback, nil)
IOHIDManagerRegisterInputValueCallback(manager, inputCallback, nil)
IOHIDManagerScheduleWithRunLoop(manager, CFRunLoopGetCurrent(), CFRunLoopMode.defaultMode.rawValue)
IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone))
FileHandle.standardError.write("hidmon started\n".data(using: .utf8)!)
CFRunLoopRun()
