"""麦克风录音：ffmpeg + avfoundation，按住说话、松手出 WAV。"""
import array
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import wave


class MicError(Exception):
    pass


def _ffmpeg() -> str:
    import shutil
    found = shutil.which("ffmpeg")
    if found:
        return found
    for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"):
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    raise MicError("没有找到 ffmpeg，请安装后重启看书宝")


def list_audio_devices() -> list[dict]:
    """解析 ffmpeg -list_devices，返回 [{index, name}]。"""
    cmd = [_ffmpeg(), "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except Exception as e:
        raise MicError(f"枚举音频设备失败：{e}") from e
    out = (p.stderr or "") + (p.stdout or "")
    devs, in_audio = [], False
    for line in out.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if "AVFoundation video devices" in line:
            in_audio = False
            continue
        if not in_audio:
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m:
            devs.append({"index": int(m.group(1)), "name": m.group(2)})
    return devs


def pick_audio_device(hint: str = "") -> dict:
    devs = list_audio_devices()
    if not devs:
        raise MicError("没有找到任何音频输入设备")
    hints = [h.lower() for h in (hint, "camera-audio", "usb camera", "2k usb") if h]
    for h in hints:
        for d in devs:
            if h in d["name"].lower():
                return d
    for d in devs:
        if "mic" in d["name"].lower() or "麦克风" in d["name"]:
            return d
    return devs[0]


def wav_stats(path: str) -> dict:
    """返回时长与峰值/均值 dBFS，用于判断是不是录到了空气。"""
    with wave.open(path, "rb") as w:
        frames, rate = w.getnframes(), w.getframerate()
        raw = w.readframes(frames)
    samples = array.array("h")
    samples.frombytes(raw)
    if not samples:
        return {"seconds": 0.0, "peak_db": -99.0, "rms_db": -99.0}
    peak = max(abs(s) for s in samples) or 1
    mean_sq = sum((s / 32768.0) ** 2 for s in samples) / len(samples)
    return {
        "seconds": round(frames / float(rate or 1), 2),
        "peak_db": round(20 * math.log10(peak / 32768.0), 1),
        "rms_db": round(10 * math.log10(mean_sq or 1e-12), 1),
    }


class Recorder:
    """一次只允许一个录音会话。"""

    def __init__(self):
        self._proc = None
        self._path = None
        self._lock = threading.Lock()
        self.device = None

    @property
    def recording(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, hint: str = "", max_seconds: int = 30, gain: float = 1.0) -> dict:
        with self._lock:
            if self.recording:
                raise MicError("正在录音中")
            self.device = pick_audio_device(hint)
            fd, path = tempfile.mkstemp(prefix="ksb_mic_", suffix=".wav")
            os.close(fd)
            self._path = path
            cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error",
                   "-f", "avfoundation", "-i", f":{self.device['index']}",
                   "-t", str(max(1, int(max_seconds))), "-ar", "16000", "-ac", "1"]
            if gain and abs(gain - 1.0) > 0.01:
                cmd += ["-af", f"volume={gain:.2f}"]
            cmd += [
                   "-c:a", "pcm_s16le", "-y", path]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            return {"device": self.device["name"], "path": path}

    def stop(self) -> dict:
        with self._lock:
            p, path = self._proc, self._path
            self._proc, self._path = None, None
        if not p:
            raise MicError("没有正在进行的录音")
        if p.poll() is None:
            try:
                p.send_signal(signal.SIGINT)  # ffmpeg 会补 WAV 尾块
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=2)
        try:
            stats = wav_stats(path)
        except Exception as e:
            raise MicError(f"录音文件无效：{e}") from e
        return {"path": path, **stats}


def write_wav(path: str, pcm: bytes, rate: int = 16000) -> str:
    """把裸 PCM（16k/16bit/单声道）写成标准 WAV，供模型直接读取。"""
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return path


class StreamRecorder:
    """常开一路麦，连续按固定时长切块。

    唤醒不能每 1 秒重启一次 ffmpeg：枚举设备 + 启动 + 收尾大约要 1~2 秒，
    中间是聋的，喊一声很容易整句掉进空档。这里一次开流，持续读 PCM 切块。
    """

    RATE = 16000
    WIDTH = 2

    def __init__(self):
        self._lock = threading.Lock()
        self._proc = None
        self._chunk_bytes = self.RATE * self.WIDTH
        self.hint = None
        self.device = None
        self.chunk_seconds = 1.0
        self.gain = 1.0

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def open(self, hint: str = "", chunk_seconds: float = 1.0, gain: float = 1.0):
        with self._lock:
            want = hint or ""
            if (self.alive and self.hint == want
                    and abs(self.chunk_seconds - chunk_seconds) < 0.05
                    and abs(self.gain - gain) < 0.01):
                return self.device
            self._close_locked()
            self.device = pick_audio_device(hint)
            self.hint = want
            self.chunk_seconds = chunk_seconds
            self.gain = gain
            self._chunk_bytes = max(1, int(self.RATE * chunk_seconds)) * self.WIDTH
            cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error",
                   "-f", "avfoundation", "-i", f":{self.device['index']}",
                   "-vn", "-ar", str(self.RATE), "-ac", "1"]
            if gain and abs(gain - 1.0) > 0.01:
                cmd += ["-af", f"volume={gain:.2f}"]
            cmd += ["-f", "s16le", "-"]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                          stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
            return self.device

    def read_pcm(self) -> bytes | None:
        """阻塞读满一块；进程退出或读不到半块就返回 None。"""
        p = self._proc
        if not p or not p.stdout:
            return None
        buf = bytearray()
        while len(buf) < self._chunk_bytes:
            try:
                b = p.stdout.read(self._chunk_bytes - len(buf))
            except Exception:
                break
            if not b:
                break
            buf += b
        if len(buf) < self._chunk_bytes // 2:
            return None
        return bytes(buf)

    def _close_locked(self):
        p, self._proc = self._proc, None
        if not p:
            return
        try:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait(timeout=2)
        except Exception:
            pass

    def close(self):
        with self._lock:
            self._close_locked()
