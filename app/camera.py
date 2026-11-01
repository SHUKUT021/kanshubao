"""单进程独占 ffmpeg 抓帧，解析 MJPEG 流，供网页预览与拍照共用。"""
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque

BUILTIN_HINTS = ("macbook", "facetime", "built-in", "内置", "desk view", "桌上视角")
# 明确不碰的设备：屏幕采集虚拟相机、桌上视角、以及 iPhone 连续互通相机（“XX的相机”）
VIRTUAL_HINTS = ("capture screen", "screen capture", "obs virtual", "虚拟",
                 "desk view", "桌上视角", "的相机", "iphone", "continuity")


def device_kind(name: str) -> str:
    """external / builtin / virtual —— 前端按这个分类，虚拟相机一律不给选。"""
    low = (name or "").lower()
    if any(k in low for k in VIRTUAL_HINTS):
        return "virtual"
    if any(k in low for k in BUILTIN_HINTS):
        return "builtin"
    return "external"


# ffmpeg 的常规提示，不能当故障上报（否则在线也会显示“取流失败”）
NON_FATAL = re.compile(
    r"Overriding selected pixel format|Last message repeated|deprecated pixel format"
    r"|Input #0|Duration:|Stream #0", re.I)

# 预览流参数：1080p + q:v 2 会让 ffmpeg 进入“疯狂重复同一帧”的失控状态
# （实测 30 秒写出 8.7GB、画面永久冻结），所以预览不用 1080p。
# 1280x960 原生只有 5~7 帧，640x480 起不来；720p 是这颗相机唯一流畅的预览模式。
LIVE_SIZES = ("1280x720", "640x480")
LIVE_QUALITY = "4"
STILL_SIZE = "1280x960"      # 1080p 单帧在本机会偶发花屏/撕裂，1280x960 是原生模式且更干净
STILL_QUALITY = "3"


class Camera:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.frame: bytes | None = None
        self.seq = 0
        self.ts = 0.0
        self.error: str | None = None
        self.notice: str | None = None
        self.device_name: str | None = None
        self.resolution: str | None = None
        self._proc: subprocess.Popen | None = None
        self._stop = False
        self._thread: threading.Thread | None = None
        self._still_lock = threading.Lock()
        self._err_lines: deque[str] = deque(maxlen=20)
        self._modes_cache: dict[str, list[str]] = {}

    # ---------- 设备枚举 ----------
    def list_devices(self) -> list[dict]:
        ff = shutil.which("ffmpeg")
        if not ff:
            return []
        try:
            out = subprocess.run(
                [ff, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                capture_output=True, text=True, timeout=15,
            ).stderr
        except Exception:
            return []
        devs, in_video = [], False
        for line in out.splitlines():
            if "AVFoundation video devices" in line:
                in_video = True
                continue
            if "AVFoundation audio devices" in line:
                in_video = False
                continue
            m = re.search(r"\[(\d+)\]\s+(.+)$", line)
            if in_video and m:
                devs.append({"index": int(m.group(1)), "name": m.group(2).strip()})
        return devs

    def _pick_device(self) -> int | None:
        devs = self.list_devices()
        chosen_name = (self.cfg.get("camera_name") or "").strip()
        self.name_missing = False
        if chosen_name:
            for d in devs:
                if d["name"] == chosen_name:
                    self.device_name = d["name"]
                    self.notice = None
                    return d["index"]
            # 设备名在枚举里找不到：索引会漂移，绝对不能拿旧索引硬开
            self.name_missing = True
        idx = self.cfg.get("camera_index")
        if isinstance(idx, int) and idx >= 0 and not chosen_name:
            return idx
        hint = (self.cfg.get("camera_name_hint") or "").lower()
        real = [d for d in devs
                if not any(k in d["name"].lower() for k in BUILTIN_HINTS + VIRTUAL_HINTS)]
        if hint:
            for d in real + devs:
                if hint in d["name"].lower():
                    self.device_name = d["name"]
                    return d["index"]
        # 优先 USB 外接真实相机；没有就退回内置，避免选到虚拟/黑屏相机
        usb = [d for d in real if any(k in d["name"].lower()
                                      for k in ("usb", "webcam", "camera", "hik", "摄像头"))]
        builtin = [d for d in devs if any(k in d["name"].lower() for k in BUILTIN_HINTS)]
        pick = (usb or builtin or real or devs)
        if pick:
            self.device_name = pick[0]["name"]
            if chosen_name:
                self.notice = f"已选「{chosen_name}」但当前没枚举到，暂时使用「{self.device_name}」"
            return pick[0]["index"]
        return None

    # ---------- 生命周期 ----------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._kill_orphans()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self, join: bool = False):
        self._stop = True
        if self._proc:
            try:
                self._proc.kill()
            except Exception:
                pass
        if join and self._thread:
            self._thread.join(timeout=3)
            self._thread = None
            with self.lock:
                self.frame = None   # 旧相机的帧不能冒充新相机的画面
                self.ts = 0.0

    def switch(self, name: str, timeout: float = 10.0) -> dict:
        """切换相机并等到真正出画面；切换和抓拍共用一把锁，避免两个 ffmpeg 抢设备。"""
        with self._still_lock:
            self.cfg["camera_name"] = (name or "").strip()
            self.cfg["camera_index"] = None
            self.error = None
            self.notice = None
            self.stop(join=True)
            time.sleep(0.4)
            self.start()
            deadline = time.time() + timeout
            while time.time() < deadline:
                st = self.status()
                if st["connected"]:
                    return st
                time.sleep(0.3)
            return self.status()

    def _kill_orphans(self):
        """清掉上次被强杀后遗留的 ffmpeg（它一直占着摄像头，导致新进程黑屏）。"""
        try:
            subprocess.run(["pkill", "-f", "ffmpeg.*avfoundation"],
                           capture_output=True, timeout=5)
            time.sleep(0.3)
        except Exception:
            pass

    def _run(self):
        while not self._stop:
            idx = self._pick_device()
            if idx is None:
                self.error = ("没拿到摄像头列表。多半是权限：打开 系统设置 → 隐私与安全性 → 摄像头，"
                              "勾选「看书宝」，然后点「重连摄像头」。仍不行再换一个 USB 口")
                time.sleep(5)
                continue
            ok = self._stream(idx)
            if not ok and not self._stop:
                tail = " | ".join(list(self._err_lines)[-3:])
                if tail:
                    self.error = f"取流失败：{tail}"
                time.sleep(2)

    def _candidates(self) -> list[str]:
        want = self.cfg.get("live_size")
        order = ([want] if want else []) + list(LIVE_SIZES)
        out = []
        for s in order:
            if s not in out:
                out.append(s)
        return out

    def _stream(self, idx: int) -> bool:
        ff = shutil.which("ffmpeg")
        fps = str(self.cfg.get("fps", 30))
        flip = ["-vf", "hflip,vflip"] if self.cfg.get("flip", True) else []
        for size in self._candidates():
            if self._stop:
                return False
            cmd = [ff, "-hide_banner", "-loglevel", "warning", "-f", "avfoundation",
                   "-framerate", fps, "-video_size", size, "-i", f"{idx}:none",
                   ] + flip + ["-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", LIVE_QUALITY, "-"]
            try:
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        bufsize=0)
            except Exception as e:
                self.error = f"启动 ffmpeg 失败：{e}"
                return False
            self._proc = proc
            threading.Thread(target=self._drain_err, args=(proc,), daemon=True).start()
            self.resolution = size
            threading.Thread(target=self._watchdog, args=(proc,), daemon=True).start()
            got = self._read_stream(proc, deadline=time.time() + 8)
            if got:
                self.error = None
                self._read_stream(proc, deadline=None)
            else:
                self.resolution = None
            try:
                proc.kill()
            except Exception:
                pass
            self._proc = None
            if got and not self._stop:
                # 掉流后重连
                continue
        return False

    def _watchdog(self, proc: subprocess.Popen):
        """6 秒没有新帧就杀掉 ffmpeg，让外层循环重连，避免画面永久冻结。"""
        last_seq, last_ts = self.seq, time.time()
        while proc.poll() is None and not self._stop:
            time.sleep(0.5)
            if self.seq != last_seq:
                last_seq, last_ts = self.seq, time.time()
            elif time.time() - last_ts > 6:
                self._err_lines.append("6 秒没有新画面，自动重启取流")
                try:
                    proc.kill()
                except Exception:
                    pass
                return

    def _drain_err(self, proc: subprocess.Popen):
        if not proc.stderr:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode(errors="replace").strip()
            if line and "frame=" not in line and not NON_FATAL.search(line):
                self._err_lines.append(line)

    def probe(self, with_modes: bool = False) -> dict:
        """列出设备与支持的分辨率（用一个非法分辨率让 ffmpeg 报出 supported modes）。"""
        ff = shutil.which("ffmpeg") or "ffmpeg"
        devs = self.list_devices()
        idx = self._pick_device()
        target = next((d for d in devs if d["index"] == idx), None)
        if target is None:
            target = devs[0] if devs else None
        modes = self._modes_cache.get(target["name"], []) if target else []
        # 只有用户主动扫描才临时让出设备；页面加载时不抢摄像头，否则会把正在用的相机关掉
        if target and with_modes and not modes:
            modes = self._read_modes(ff, target)
        for d in devs:
            d["kind"] = device_kind(d["name"])
        return {"devices": devs, "target": target, "modes": modes[:20],
                "current": self.status(),
                "configured_name": (self.cfg.get("camera_name") or "").strip(),
                "external": bool(target and device_kind(target["name"]) == "external")}

    def _read_modes(self, ff: str, target: dict) -> list[str]:
        """临时停流抓一次 supported modes，再恢复取流。"""
        modes: list[str] = []
        with self._still_lock:
            was_live = self.status()["connected"]
            if was_live:
                self.stop(join=True)
                time.sleep(0.3)
            cmd = [ff, "-hide_banner", "-f", "avfoundation", "-video_size", "9999x9999",
                   "-i", f"{target['index']}:none", "-f", "null", "-"]
            try:
                out = subprocess.run(cmd, capture_output=True, text=True, timeout=25).stderr
            except Exception as e:
                out = str(e)
            on = False
            for line in out.splitlines():
                if "Supported modes" in line:
                    on = True
                    continue
                if on:
                    m = re.search(r"(\d{3,5}x\d{3,5})", line)
                    if m:
                        if m.group(1) not in modes:
                            modes.append(m.group(1))
                    elif line.strip():
                        break
            if was_live:
                self.start()
        if modes:
            self._modes_cache[target["name"]] = modes
        return modes

    def _read_stream(self, proc: subprocess.Popen, deadline: float | None) -> bool:
        buf = bytearray()
        any_frame = False
        win_start, win_frames = time.time(), 0
        while not self._stop:
            if deadline and time.time() > deadline:
                return any_frame
            chunk = proc.stdout.read(65536) if proc.stdout else b""
            if not chunk:
                return any_frame
            buf += chunk
            while True:
                s = buf.find(b"\xff\xd8")
                if s < 0:
                    if len(buf) > 4_000_000:
                        buf.clear()
                    break
                e = buf.find(b"\xff\xd9", s + 2)
                if e < 0:
                    if s > 0:
                        del buf[:s]
                    break
                jpg = bytes(buf[s:e + 2])
                del buf[:e + 2]
                with self.lock:
                    self.frame = jpg
                    self.seq += 1
                    self.ts = time.time()
                any_frame = True
                win_frames += 1
            now = time.time()
            if now - win_start >= 1.0:
                # 超过 100 帧/秒＝ffmpeg 在重复同一帧空转，画面其实早已卡死
                if win_frames > 100:
                    self._err_lines.append(f"取流失控：{win_frames} 帧/秒，已重启")
                    return False
                win_start, win_frames = now, 0

    # ---------- 高清抓拍 ----------
    def capture_still(self, timeout: float = 20.0) -> bytes:
        """暂停预览，单帧抓拍（默认 1280x960），再恢复预览。用于批改。"""
        with self._still_lock:
            self.stop(join=True)
            ff = shutil.which("ffmpeg")
            idx = self._pick_device()
            if idx is None:
                self.start()
                raise RuntimeError(self.error or "没有可用摄像头")
            with tempfile.TemporaryDirectory() as td:
                dst = os.path.join(td, "still.jpg")
                cmd = [ff, "-hide_banner", "-loglevel", "error", "-f", "avfoundation",
                       "-framerate", str(self.cfg.get("fps", 30)),
                       "-video_size", self.cfg.get("still_size") or STILL_SIZE,
                       "-i", f"{idx}:none",
                       ] + (["-vf", "hflip,vflip"] if self.cfg.get("flip", True) else []) + [
                       "-frames:v", "1", "-q:v", STILL_QUALITY, "-y", dst]
                try:
                    subprocess.run(cmd, capture_output=True, timeout=timeout)
                    if os.path.exists(dst) and os.path.getsize(dst) > 10_000:
                        with open(dst, "rb") as f:
                            return f.read()
                    raise RuntimeError("抓拍失败：没有取到画面")
                finally:
                    self.start()

    # ---------- 取帧 ----------
    def snapshot(self, fresh: bool = True, timeout: float = 6.0) -> bytes:
        with self.lock:
            before = self.seq
            frame = self.frame
        if frame and not fresh:
            return frame
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                if self.seq > before and self.frame:
                    return self.frame
            time.sleep(0.05)
        if frame:
            return frame
        raise RuntimeError(self.error or "摄像头没有画面")

    def status(self) -> dict:
        with self.lock:
            alive = bool(self.frame) and (time.time() - self.ts) < 5
        err = self.error
        if not alive and not err:
            tail = " | ".join(list(self._err_lines)[-3:])
            err = f"取流失败：{tail}" if tail else "摄像头无画面"
        return {
            "connected": alive,
            "device": self.device_name,
            "kind": device_kind(self.device_name or "") if self.device_name else None,
            "resolution": self.resolution,
            "frames": self.seq,
            "age": round(time.time() - self.ts, 1) if self.ts else None,
            "error": None if alive else err,
            "notice": self.notice,
        }
