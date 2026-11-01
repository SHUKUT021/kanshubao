"""豆包语音合成（火山引擎语音技术）与本地播放。

方舟 Ark 的 API Key 只负责大模型，不等同于语音合成凭证；
语音合成需要「语音技术」控制台里创建应用后拿到的 AppID + Access Token。
固定短句（例如唤醒应答「我在」）会落盘缓存，避免每次重新请求。
"""
import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
CACHE_DIR = os.path.join(PROJ, "cache", "tts")

ENDPOINT = "https://openspeech.bytedance.com/api/v1/tts"
DEFAULT_VOICE = "zh_female_shuangkuaisisi_moon_bigtts"
VOICES = [
    ("zh_female_shuangkuaisisi_moon_bigtts", "爽快思思 · 女声（推荐）"),
    ("zh_female_vv_moon_bigtts", "VV · 女声"),
    ("zh_female_wanwanxiaohe_moon_bigtts", "湾湾小何 · 女声"),
    ("zh_male_yunzhoujieshuo_moon_bigtts", "云舟 · 男声"),
]

_play_lock = threading.Lock()


class TTSError(Exception):
    pass


def available(cfg: dict) -> bool:
    return bool((cfg.get("tts_appid") or "").strip()
                and (cfg.get("tts_token") or "").strip())


def voice_type(cfg: dict) -> str:
    return (cfg.get("tts_voice") or DEFAULT_VOICE).strip()


def _cache_path(cfg: dict, text: str) -> str:
    key = "\x1f".join((
        (cfg.get("tts_appid") or "").strip(),
        voice_type(cfg),
        (cfg.get("tts_cluster") or "volcano_tts").strip(),
        f"{float(cfg.get('tts_speed') or 1.0):.2f}",
        text.strip(),
    ))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(CACHE_DIR, digest + ".mp3")


def synth(cfg: dict, text: str, timeout: int = 25) -> bytes:
    """返回 mp3 字节。失败抛 TTSError。"""
    appid = (cfg.get("tts_appid") or "").strip()
    token = (cfg.get("tts_token") or "").strip()
    if not (appid and token):
        raise TTSError("未配置豆包语音 AppID / Access Token")
    body = {
        "app": {"appid": appid, "token": token,
                "cluster": (cfg.get("tts_cluster") or "volcano_tts").strip()},
        "user": {"uid": "kanshubao"},
        "audio": {"voice_type": voice_type(cfg),
                  "encoding": "mp3",
                  "speed_ratio": float(cfg.get("tts_speed") or 1.0),
                  "rate": 24000},
        "request": {"reqid": str(uuid.uuid4()), "text": text[:600],
                    "operation": "query"},
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer;{token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise TTSError(f"语音合成 {e.code}：{detail}") from e
    except urllib.error.URLError as e:
        raise TTSError(f"连不上语音合成：{e.reason}") from e
    except Exception as e:
        raise TTSError(f"语音合成请求失败：{e}") from e
    code = data.get("code")
    if code not in (0, 3000, None):
        raise TTSError(f"语音合成失败 code={code} {str(data.get('message'))[:160]}")
    b64 = data.get("data")
    if not b64:
        raise TTSError(f"返回没有音频：{str(data)[:200]}")
    return base64.b64decode(b64)


def synth_cached(cfg: dict, text: str, timeout: int = 25) -> str:
    """返回本地 mp3 路径；固定短句会复用缓存。"""
    text = (text or "").strip()
    if not text:
        raise TTSError("朗读内容为空")
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(cfg, text)
    if os.path.exists(path) and os.path.getsize(path) > 512:
        return path
    audio = synth(cfg, text, timeout=timeout)
    if len(audio) < 128:
        raise TTSError("语音合成返回的音频为空")
    fd, tmp = tempfile.mkstemp(prefix="ksb_tts_", suffix=".mp3", dir=CACHE_DIR)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return path


def warm(cfg: dict, phrases: list[str] | None = None) -> dict:
    """预合成固定短句，让唤醒应答的第一声几乎立刻出来。"""
    result = {"ok": True, "cached": [], "errors": []}
    if not available(cfg):
        result.update(ok=False, errors=["未配置豆包语音 AppID / Access Token"])
        return result
    for text in (phrases or ["我在"]):
        try:
            synth_cached(cfg, text)
            result["cached"].append(text)
        except TTSError as e:
            result["errors"].append(f"{text}: {e}")
    result["ok"] = not result["errors"]
    return result


def _say_fallback(cfg: dict, text: str):
    cmd = ["say"]
    voice = (cfg.get("voice") or "").strip()
    if voice:
        cmd += ["-v", voice]
    try:
        subprocess.run(cmd + [text[:400]], timeout=90,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def speak_blocking(cfg: dict, text: str, fallback: bool = False) -> bool:
    """朗读并等它念完。返回是否用上了豆包音色。"""
    text = (text or "").strip()
    if not text:
        return False
    used_doubao = False
    if available(cfg):
        try:
            path = synth_cached(cfg, text)
            with _play_lock:
                subprocess.run(["afplay", path], timeout=120,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            used_doubao = True
        except Exception:
            used_doubao = False
    if not used_doubao and fallback:
        _say_fallback(cfg, text)
    return used_doubao
