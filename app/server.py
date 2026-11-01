"""看书宝 Mac 版本地服务：摄像头预览 + 作业批改 + 答疑。只用标准库。"""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import ark
import asr
import camera as camera_mod
import mic
import memory
import prompts
import realtime
import tts
import voice
from camera import Camera

# Finder-launched apps do not inherit the user's shell PATH.
os.environ["PATH"] = os.pathsep.join(dict.fromkeys(
    os.environ.get("PATH", "").split(os.pathsep)
    + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"]
))

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(ROOT)
CONFIG_PATH = os.path.join(PROJ, "config.json")
STATIC = os.path.join(ROOT, "static")
LOG_DIR = os.path.join(PROJ, "logs")
HID_BIN = os.path.join(PROJ, "tools", "hidmon")
MEMORY = memory.Memory(os.path.join(PROJ, "data", "memory.sqlite3"))


def load_config() -> dict:
    defaults = {
        "provider": "ark",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "doubao-seed-1-6-250615",
        "api_key": "",
        "grade": "上海小学六年级",
        "subjects": "语文、数学、英语",
        "camera_index": None,
        "camera_name": "",
        "camera_name_hint": "USB",
        "video_size": "1920x1080",
        "still_size": "1280x960",
        "flip": True,
        "fps": 30,
        "port": 8770,
        "voice": "Tingting",
        "tts_appid": "",
        "tts_token": "",
        "tts_cluster": "volcano_tts",
        "tts_voice": "zh_female_shuangkuaisisi_moon_bigtts",
        "tts_speed": 1.05,
        "wake_reply": "我在",
        "wake_chunk_seconds": 1.0,
        "wake_min_db": -48,
        "asr_enabled": True,
        "asr_key": "",
        "asr_appid": "",
        "asr_token": "",
        "asr_url": "",
        "asr_resource_id": "",
        "rt_api_key": "",
        "rt_speaker": realtime.DEFAULT_SPEAKER,
        "rt_model": "2.2.0.0",
        "rt_barge_in": False,
        "rt_keyword_interrupt": True,
        "rt_echo_guard_ms": 600,
        "rt_url": "",
        "rt_hello": "我在，有不会的题直接问我。",
        "followup_enabled": True,
        "followup_seconds": 8,
        "followup_turns": 2,
        "question_chunk_seconds": 0.9,
        "question_silence_chunks": 2,
        "question_max_chunks": 9,
        "audio_hint": "Camera-Audio",
        "mic_gain": 2.0,
        "thinking": "disabled",
        "voice_with_image": True,
        "wake_enabled": False,
        "hid_enabled": True,
        "hid_match": "",
        "hid_map": {},
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            defaults.update(json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[config] 读取失败，使用默认值：{e}")
    return defaults


CFG = load_config()
CAM = Camera(CFG)
REC = mic.Recorder()


def speak_blocking(text: str):
    """豆包音色朗读并等它念完。"""
    text = (text or "").strip()
    if not text:
        return
    tts.speak_blocking(CFG, text)


VA = voice.VoiceAssistant(
    CFG,
    snapshot=lambda: prepare_image(CAM.snapshot()),
    speak_blocking=speak_blocking,
    busy_check=lambda: REC.recording,
    recorder=REC,
)
_config_lock = threading.Lock()
VA.on_round = lambda q, a: MEMORY.save(MEMORY.setting("chat"), q, a)
VA.memory_context = lambda: MEMORY.fact_prompt() + "\n近期对话资料：" + json.dumps(MEMORY.context(), ensure_ascii=False)
if MEMORY.setting("enabled", "1") == "1":
    VA.history = MEMORY.history()

# 端到端实时语音：同一时刻只允许一场通话（它独占麦克风）
RT = None
RT_LOCK = threading.RLock()

# 摄像头物理按键事件（只来自被指定的摄像头设备）
EVENTS: deque = deque(maxlen=50)
EVENT_LOCK = threading.Lock()
EVENT_SEQ = 0


def push_event(obj: dict):
    global EVENT_SEQ
    with EVENT_LOCK:
        EVENT_SEQ += 1
        obj = dict(obj, id=EVENT_SEQ, ts=time.strftime("%H:%M:%S"))
        EVENTS.append(obj)


def hid_loop():
    """按摄像头名字作用域启动 hidmon；设备未就绪就等。"""
    if not os.path.exists(HID_BIN):
        return
    while True:
        match = (CFG.get("hid_match") or CAM.device_name or "").strip()
        if not CFG.get("hid_enabled", True) or not match:
            time.sleep(3)
            continue
        try:
            proc = subprocess.Popen([HID_BIN, match], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except Exception:
            time.sleep(5)
            continue
        for line in proc.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("type") == "button":
                push_event(ev)
        try:
            proc.wait(timeout=1)
        except Exception:
            proc.kill()
        time.sleep(3)


def save_config(patch: dict):
    with _config_lock:
        for k, v in patch.items():
            if k in CFG and k not in ("port",):
                CFG[k] = v
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CONFIG_PATH)
    CAM.cfg = CFG


# ---------------- 图像处理 ----------------
def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def decode_image(data: str) -> bytes:
    if not data:
        raise ValueError("没有图片")
    if data.startswith("data:"):
        data = data.split(",", 1)[-1]
    try:
        return base64.b64decode(data)
    except Exception as e:
        raise ValueError(f"图片解码失败：{e}") from e


def prepare_image(raw: bytes, max_side: int = 2600) -> bytes:
    """按 EXIF 转正、限制长边、统一 JPEG（手写批改清晰度与体积的平衡点）。"""
    with tempfile.TemporaryDirectory() as td:
        src, dst = os.path.join(td, "in"), os.path.join(td, "out.jpg")
        with open(src, "wb") as f:
            f.write(raw)
        cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", src,
               "-vf", f"scale='if(gt(iw,ih),min({max_side},iw),-2)':'if(gt(iw,ih),-2,min({max_side},ih))'",
               "-q:v", "3", dst]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.exists(dst):
            with open(dst, "rb") as f:
                return f.read()
    return raw


def split_halves(jpeg: bytes) -> list[bytes]:
    """上下两段带 10% 重叠，提升手写小字识别率。"""
    out = []
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "in.jpg")
        with open(src, "wb") as f:
            f.write(jpeg)
        for i, crop in enumerate(["crop=iw:ih*0.56:0:0", "crop=iw:ih*0.56:0:ih*0.44"]):
            dst = os.path.join(td, f"part{i}.jpg")
            cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                   "-vf", crop, "-q:v", "3", dst]
            r = subprocess.run(cmd, capture_output=True, timeout=60)
            if r.returncode == 0 and os.path.exists(dst):
                with open(dst, "rb") as f:
                    out.append(f.read())
    return out


def merge_results(results: list[dict]) -> dict:
    items, seen = [], set()
    for r in results:
        for it in r.get("items", []) or []:
            no = str(it.get("no", "")).strip()
            key = no or f"?{len(items)}"
            if key in seen:
                continue
            seen.add(key)
            items.append(it)
    counts = {"total": len(items), "correct": 0, "wrong": 0, "unclear": 0}
    for it in items:
        v = it.get("verdict")
        if v in counts:
            counts[v] += 1
    return {"subject": results[0].get("subject", "?") if results else "?",
            "items": items, "counts": counts,
            "notes": " ".join(r.get("notes", "") for r in results if r.get("notes"))[:200]}


def log_check(result: dict, mode: str, src: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "source": src,
           "counts": result.get("counts"), "items": result.get("items")}
    with open(os.path.join(LOG_DIR, "checks.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def speak(text: str):
    text = (text or "").strip()
    if not text:
        return
    with RT_LOCK:
        if RT is not None and RT.state().get("running"):
            RT.say(text, record=False)
            return
    threading.Thread(target=tts.speak_blocking, args=(CFG, text), daemon=True).start()


# ---------------- API ----------------
def api_check(body: dict) -> dict:
    mode = body.get("mode") or "full"
    source = "upload"
    if body.get("image"):
        raw = decode_image(body["image"])
    else:
        try:
            raw = CAM.capture_still()   # 1080p 单帧，小字更清楚
        except Exception:
            raw = CAM.snapshot()
        source = "camera"
    img = prepare_image(raw)
    system, user = prompts.check_messages(CFG, mode)

    split = int(body.get("split") or 0)
    parts = split_halves(img) if split else []
    results = []
    if parts:
        for p in parts:
            txt = ark.chat(CFG, system, user, p)
            results.append(ark.extract_json(txt))
        result = merge_results(results)
    else:
        txt = ark.chat(CFG, system, user, img)
        result = ark.extract_json(txt)
        if not result.get("counts"):
            result = merge_results([result])
    log_check(result, mode, source)
    return {"ok": True, "result": result, "source": source,
            "image_bytes": len(img), "tiles": len(parts) or 1}


def api_answer(body: dict) -> dict:
    q = (body.get("question") or "").strip()
    if body.get("image"):
        img = prepare_image(decode_image(body["image"]))
    else:
        try:
            img = prepare_image(CAM.snapshot())
        except Exception:
            img = None
    system, user = prompts.answer_messages(CFG, q)
    text = ark.chat(CFG, system, user, img, max_tokens=600, temperature=0.3)
    if body.get("speak", True):
        speak(text)
    return {"ok": True, "text": text.strip()}


def api_config(body: dict) -> dict:
    patch = {k: v for k, v in body.items() if k in CFG}
    if "api_key" in patch:
        patch["api_key"] = clean_key(patch["api_key"])
        if patch["api_key"]:
            bad = key_shape_problem(patch["api_key"])
            if bad:
                return {"ok": False, "error": bad}
    if "model" in patch:
        patch["model"] = (patch["model"] or "").strip()
    if "tts_appid" in patch:
        patch["tts_appid"] = str(patch["tts_appid"] or "").strip()
    if "tts_token" in patch:
        patch["tts_token"] = clean_key(patch["tts_token"])
    if "tts_voice" in patch:
        patch["tts_voice"] = str(patch["tts_voice"] or "").strip()
    if "asr_key" in patch:
        patch["asr_key"] = clean_key(patch["asr_key"])
    if "asr_appid" in patch:
        patch["asr_appid"] = str(patch["asr_appid"] or "").strip()
    if "asr_token" in patch:
        patch["asr_token"] = clean_key(patch["asr_token"])
    if "asr_url" in patch:
        patch["asr_url"] = str(patch["asr_url"] or "").strip()
    if "asr_resource_id" in patch:
        patch["asr_resource_id"] = str(patch["asr_resource_id"] or "").strip()
    if "rt_api_key" in patch:
        patch["rt_api_key"] = clean_key(patch["rt_api_key"])
        k = patch["rt_api_key"]
        if k and k.lower().startswith("ark-"):
            return {"ok": False, "error": "这是火山方舟的 ark- Key，只能调大模型，不能用于实时语音。"
                                             "请到豆包语音控制台 → API Key 管理，复制 36 位 UUID 的 Key。"}
        if k and len(k) < 20:
            return {"ok": False, "error": f"语音 Key 只有 {len(k)} 位，豆包语音 Key 通常是 36 位 UUID，请确认复制完整。"}
    if "rt_speaker" in patch:
        patch["rt_speaker"] = str(patch["rt_speaker"] or "").strip() or realtime.DEFAULT_SPEAKER
    if "rt_model" in patch:
        patch["rt_model"] = str(patch["rt_model"] or "").strip() or "2.2.0.0"
    if "rt_hello" in patch:
        patch["rt_hello"] = str(patch["rt_hello"] or "").strip()[:40]
    if "rt_echo_guard_ms" in patch:
        try:
            patch["rt_echo_guard_ms"] = max(0, min(3000, int(patch["rt_echo_guard_ms"])))
        except (TypeError, ValueError):
            patch.pop("rt_echo_guard_ms")
    if "rt_barge_in" in patch:
        patch["rt_barge_in"] = bool(patch["rt_barge_in"])
    if "rt_keyword_interrupt" in patch:
        patch["rt_keyword_interrupt"] = bool(patch["rt_keyword_interrupt"])
    if "wake_reply" in patch:
        patch["wake_reply"] = str(patch["wake_reply"] or "我在").strip()[:20] or "我在"
    if "audio_hint" in patch:
        patch["audio_hint"] = str(patch["audio_hint"] or "").strip()
    if "mic_gain" in patch:
        try:
            patch["mic_gain"] = max(0.5, min(8.0, float(patch["mic_gain"])))
        except (TypeError, ValueError):
            patch.pop("mic_gain")
    if "wake_min_db" in patch:
        try:
            patch["wake_min_db"] = max(-70.0, min(-20.0, float(patch["wake_min_db"])))
        except (TypeError, ValueError):
            patch.pop("wake_min_db")
    if "followup_seconds" in patch:
        try:
            patch["followup_seconds"] = max(3.0, min(30.0, float(patch["followup_seconds"])))
        except (TypeError, ValueError):
            patch.pop("followup_seconds")
    if "followup_turns" in patch:
        try:
            patch["followup_turns"] = max(0, min(5, int(patch["followup_turns"])))
        except (TypeError, ValueError):
            patch.pop("followup_turns")
    save_config(patch)
    out = {"ok": True, "config": public_config()}
    if patch.get("api_key"):
        try:
            system, user = prompts.answer_messages(CFG, "只回四个字：连接正常")
            txt = ark.chat(CFG, system, user, None, max_tokens=50, temperature=0)
            out["test"] = {"ok": True, "text": txt.strip()[:80]}
        except ark.ProviderError as e:
            out["test"] = {"ok": False, "error": str(e)[:400]}
    if any(k.startswith("tts_") for k in patch) and tts.available(CFG):
        out["tts_test"] = tts.warm(CFG, [CFG.get("wake_reply") or "我在"])
    return out


def clean_key(raw: str) -> str:
    """粘贴来的 Key 常带换行、引号或 Bearer 前缀，统一清洗。"""
    k = (raw or "").strip().strip("'\"").strip()
    if k.lower().startswith("bearer "):
        k = k[7:].strip()
    return "".join(k.split())


def key_shape_problem(k: str) -> str:
    """识别最常见的填错：把模型名/接入点 ID 粘进了 Key 栏。"""
    low = k.lower()
    if low.startswith("ep-") or "doubao" in low or low.startswith("deepseek") \
            or low.startswith("glm") or low.startswith("kimi"):
        return ("这看起来是模型名或接入点 ID，不是 API Key。"
                "模型请填在下面「模型 / 接入点 ID」一栏；"
                "Key 请到方舟「API Key 管理」复制，形如 xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx。")
    if len(k) < 20:
        return f"Key 只有 {len(k)} 个字符，方舟的 Key 通常是 36 位（UUID 格式），请确认复制完整。"
    return ""


def public_config() -> dict:
    c = dict(CFG)
    key = ark.api_key(CFG)
    c["api_key_set"] = bool(key)
    c.pop("api_key", None)
    if key:
        c["api_key_hint"] = ("••••" + key[-4:]) if len(key) > 12 else "••••"
    token = (c.pop("tts_token", "") or "").strip()
    c["tts_token_set"] = bool(token)
    if token:
        c["tts_token_hint"] = ("••••" + token[-4:]) if len(token) > 8 else "••••"
    c["tts_ready"] = tts.available(CFG)
    asr_key = (c.pop("asr_key", "") or "").strip()
    c["asr_key_set"] = bool(asr_key)
    if asr_key:
        c["asr_key_hint"] = ("••••" + asr_key[-4:]) if len(asr_key) > 8 else "••••"
    asr_token = (c.pop("asr_token", "") or "").strip()
    c["asr_token_set"] = bool(asr_token)
    if asr_token:
        c["asr_token_hint"] = ("••••" + asr_token[-4:]) if len(asr_token) > 8 else "••••"
    c["asr_ready"] = asr.available(CFG)
    rt_key = (c.pop("rt_api_key", "") or "").strip()
    c["rt_key_set"] = bool(rt_key)
    if rt_key:
        c["rt_key_hint"] = ("••••" + rt_key[-4:]) if len(rt_key) > 8 else "••••"
    c["rt_ready"] = realtime.available(CFG)
    c["rt_available"] = realtime.available(CFG)
    c["rt_speakers"] = [{"id": k, "name": v} for k, v in realtime.SPEAKERS]
    return c


def api_test() -> dict:
    system, user = prompts.answer_messages(CFG, "只回四个字：连接正常")
    text = ark.chat(CFG, system, user, None, max_tokens=50, temperature=0)
    return {"ok": True, "text": text.strip()[:120]}


def api_camera_switch(body: dict) -> dict:
    """切换摄像头：只认设备名（索引会漂移），切完回报实际在用的设备。"""
    want = str(body.get("name") or "").strip()
    devs = CAM.list_devices()
    if want:
        match = next((d for d in devs if d["name"] == want), None)
        if match is None:
            return {"ok": False, "devices": devs,
                    "error": f"没找到「{want}」。插紧 USB 后点「扫描外置」再看一次。"}
        if camera_mod.device_kind(want) == "virtual":
            return {"ok": False, "devices": devs,
                    "error": f"「{want}」是屏幕/连续互通虚拟相机，拍不了桌面作业，换一个。"}
    save_config({"camera_name": want, "camera_index": None})
    st = CAM.switch(want)
    return {"ok": True, "matched": bool(st["connected"]), "camera": st, "devices": devs,
            "config": public_config()}


def api_tts_test(body: dict) -> dict:
    if not tts.available(CFG):
        return {"ok": False, "error": "还没配置火山「语音技术」的 AppID 和 Access Token。"
                                    "方舟的 API Key 不能用于语音合成，这是两项不同的服务。"}
    text = (body.get("text") or "你好，我是小豆，豆包音色已经接好了。").strip()[:100]
    t0 = time.time()
    try:
        path = tts.synth_cached(CFG, text)
        synth_seconds = round(time.time() - t0, 2)
        subprocess.run(["afplay", path], timeout=40,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "text": text, "voice": tts.voice_type(CFG),
                "synth_seconds": synth_seconds,
                "cached": path}
    except tts.TTSError as e:
        return {"ok": False, "error": str(e)[:500]}
    except Exception as e:
        return {"ok": False, "error": f"播放失败：{type(e).__name__}: {e}"}


def api_tts_status() -> dict:
    cache_files = 0
    try:
        cache_files = len([n for n in os.listdir(tts.CACHE_DIR) if n.endswith(".mp3")])
    except OSError:
        pass
    return {"ok": True, "ready": tts.available(CFG),
            "voice": tts.voice_type(CFG), "cache_files": cache_files}


def api_asr_status() -> dict:
    headers, err = asr.credentials(CFG)
    return {"ok": True, "ready": asr.available(CFG), "hint": err,
            "auth": ("api_key" if "X-Api-Key" in headers else ("appid" if headers else "")),
            "url": (CFG.get("asr_url") or asr.DEFAULT_URL),
            "resource_id": (CFG.get("asr_resource_id") or asr.DEFAULT_RESOURCE)}


def api_asr_test(body: dict) -> dict:
    """录几秒麦克风，直接打流式识别接口，看出字速度和准确率。"""
    if not asr.available(CFG):
        return {"ok": False, "error": asr.credentials(CFG)[1]}
    try:
        seconds = max(1, min(10, int(body.get("seconds") or 3)))
    except (TypeError, ValueError):
        seconds = 3
    VA.hold_mic()
    path = None
    try:
        REC.start(CFG.get("audio_hint") or "", seconds + 1,
                  gain=float(CFG.get("mic_gain") or 1.0))
        time.sleep(seconds)
        st = REC.stop()
        path = st["path"]
        t0 = time.time()
        text = asr.transcribe_wav_file(CFG, path, timeout=10.0)
        return {"ok": True, "text": text, "seconds": round(time.time() - t0, 2),
                "record_seconds": st["seconds"], "peak_db": st["peak_db"],
                "device": st.get("device")}
    except Exception as e:              # noqa: BLE001 - 一律回报给界面
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        VA.release_mic()


# ---------------- 语音：按住说话 ----------------
def api_listen_start(body: dict) -> dict:
    VA.hold_mic()
    try:
        info = REC.start(CFG.get("audio_hint") or "", int(body.get("max_seconds") or 30),
                         gain=float(CFG.get("mic_gain") or 1.0))
    except Exception:
        VA.release_mic()
        raise
    return {"ok": True, "device": info.get("device")}


def api_listen_stop(body: dict) -> dict:
    path = None
    try:
        st = REC.stop()
        path = st["path"]
        if st["seconds"] < 0.4:
            return {"ok": False, "error": "按住时间太短，说一句话至少要半秒。", **st}
        if st["peak_db"] < -48:
            return {"ok": False, "error": f"没听到声音（峰值 {st['peak_db']} dB）。检查摄像头麦克风是否被静音，或离远了一点。", **st}
        with open(path, "rb") as f:
            wav = f.read()
        heard = {"q": "", "via": ""}
        # 先走流式识别（一次灌完也是同一条通道），通常 0.5~1.5 秒出字；
        # 没配凭证或失败才退回「整段音频丢给多模态模型」。
        t_asr = time.time()
        if asr.available(CFG):
            try:
                heard["q"] = asr.transcribe_wav_file(CFG, path, timeout=8.0)
                heard["via"] = "asr"
            except Exception as e:      # noqa: BLE001 - 识别失败不影响问答
                heard["asr_error"] = f"{type(e).__name__}: {e}"[:160]
        heard["asr_seconds"] = round(time.time() - t_asr, 2)
        trans_thread = None
        if not heard["q"]:
            def _transcribe():
                try:
                    s, u = prompts.transcribe_messages()
                    heard["q"] = ark.chat(CFG, s, u, None, max_tokens=80, temperature=0,
                                          timeout=30, audio_wav=wav, thinking="disabled").strip()
                    heard["via"] = "llm"
                except Exception:
                    pass
            trans_thread = threading.Thread(target=_transcribe, daemon=True)
            trans_thread.start()
        img = None
        if body.get("with_image", CFG.get("voice_with_image", True)):
            try:
                img = prepare_image(CAM.snapshot())
            except Exception:
                img = None
        said = (heard.get("q") or "").strip()
        if said:
            system, user = prompts.voice_messages_text(CFG, said)
            audio = None
        else:
            system, user = prompts.voice_messages(CFG)
            audio = wav
        system += VA.memory_context()
        t0 = time.time()
        text = ark.chat(CFG, system, user, img, max_tokens=220, temperature=0.3,
                        timeout=90, audio_wav=audio,
                        thinking=CFG.get("thinking") or "disabled")
        elapsed = round(time.time() - t0, 1)
        text = text.strip()
        if trans_thread:
            trans_thread.join(timeout=6)
        VA.note_exchange(said or heard.get("q") or "（按住说话提问）", text)
        if body.get("speak", True) and text:
            speak(text)
        return {"ok": True, "text": text, "seconds": st["seconds"],
                "peak_db": st["peak_db"], "elapsed": elapsed,
                "heard": said or heard.get("q") or "", "asr_via": heard.get("via"),
                "asr_seconds": heard.get("asr_seconds"),
                "asr_error": heard.get("asr_error")}
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
        VA.release_mic()


# ---------------- 实时语音：端到端全双工 ----------------
def api_rt_start(body: dict) -> dict:
    """开一场实时通话：麦克风直连豆包，模型自己判断什么时候开口。"""
    global RT
    if not realtime.available(CFG):
        return {"ok": False, "error": "还没配「实时语音 API Key」。到设置 → 实时语音，"
                                     "粘贴豆包语音控制台的 36 位 Key（ark- 开头的不行）。"}
    with RT_LOCK:
        if RT is not None and RT.state().get("running"):
            return {"ok": True, "already": True, **RT.state()}
        VA.set_enabled(False)      # 和「小豆小豆」唤醒互斥，避免两路抢麦克风
        VA.hold_mic()
        chat_id = MEMORY.setting("chat")
        rt_cfg = dict(CFG, _memory_context=MEMORY.context(), _memory_facts=MEMORY.fact_prompt())
        rt = realtime.RealtimeDialogue(
            rt_cfg, on_round=lambda q, a: MEMORY.save(chat_id, q, a))
        if MEMORY.setting("enabled", "1") == "1":
            rt.history = MEMORY.history(chat_id)
        rt.cam = CAM
        hello = str(body.get("hello") or CFG.get("rt_hello") or "").strip()[:40]
        try:
            rt.start(hello)
        except Exception as e:     # noqa: BLE001 - 一律回报给界面
            try:
                rt.stop()
            except Exception:
                pass
            VA.release_mic()
            return {"ok": False, "error": f"{type(e).__name__}: {e}"[:400]}
        RT = rt
        return {"ok": True, **rt.state()}


def api_rt_stop() -> dict:
    global RT
    with RT_LOCK:
        rt, RT = RT, None
        if rt is not None:
            try:
                rt.stop()
            except Exception:
                pass
        VA.release_mic()
    return {"ok": True, "running": False}


def api_rt_status() -> dict:
    with RT_LOCK:
        rt = RT
        if rt is None:
            return {"ok": True, "running": False, "phase": "off",
                    "available": realtime.available(CFG),
                    "hello": CFG.get("rt_hello") or "",
                    "speaker": CFG.get("rt_speaker") or realtime.DEFAULT_SPEAKER}
        st = rt.state()
    st.update({"ok": True, "available": realtime.available(CFG),
               "barge_in": bool(CFG.get("rt_barge_in")),
               "hello": CFG.get("rt_hello") or ""})
    return st


def api_rt_say(body: dict) -> dict:
    text = str(body.get("text") or "").strip()[:300]
    if not text:
        return {"ok": False, "error": "说点什么吧"}
    with RT_LOCK:
        rt = RT
        if rt is None or not rt.state().get("running"):
            return {"ok": False, "error": "实时通话还没开"}
        rt.say(text)
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "KanShuBao/1.0"

    def log_message(self, fmt, *args):
        if "/stream" not in (args[0] if args else ""):
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ---------- 工具 ----------
    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    # ---------- GET ----------
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/memory":
            return self._json({"ok": True, **MEMORY.state()})
        if path in ("/", "/index.html"):
            return self._file(os.path.join(STATIC, "index.html"), "text/html; charset=utf-8")
        if path == "/api/status":
            return self._json({"camera": CAM.status(), "config": public_config()})
        if path == "/api/devices":
            return self._json({"devices": CAM.list_devices()})
        if path == "/api/camera/probe":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            with_modes = (qs.get("modes") or ["0"])[0] in ("1", "true")
            return self._json(CAM.probe(with_modes=with_modes))
        if path == "/api/mic/devices":
            try:
                devs = mic.list_audio_devices()
                return self._json({"ok": True, "devices": devs,
                                   "picked": mic.pick_audio_device(CFG.get("audio_hint") or "")})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)})
        if path == "/api/voice/state":
            return self._json({"ok": True, **VA.state()})
        if path == "/api/tts/status":
            return self._json(api_tts_status())
        if path == "/api/asr/status":
            return self._json(api_asr_status())
        if path == "/api/rt/status":
            return self._json(api_rt_status())
        if path == "/api/events":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            after = int((qs.get("after") or ["0"])[0] or 0)
            with EVENT_LOCK:
                evs = [e for e in EVENTS if e["id"] > after]
                last = EVENT_SEQ
            return self._json({"events": evs, "last": last,
                               "enabled": bool(CFG.get("hid_enabled", True) and (CFG.get("hid_match") or ""))})
        if path == "/api/frame":
            try:
                frame = CAM.snapshot(fresh=False)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(frame)
        if path == "/api/still":
            try:
                frame = CAM.capture_still()
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 503)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(frame)
        if path == "/stream.mjpg":
            return self._stream()
        return self._json({"ok": False, "error": "not found"}, 404)

    def _file(self, p: str, ctype: str):
        if not os.path.exists(p):
            return self._json({"ok": False, "error": "missing"}, 404)
        with open(p, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = 0
        try:
            while True:
                frame = CAM.snapshot(fresh=False) if CAM.frame else None
                if frame and CAM.seq != last:
                    last = CAM.seq
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                                     + str(len(frame)).encode() + b"\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    # ---------- POST ----------
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        try:
            if path == "/api/memory":
                action = body.get("action")
                if action not in ("new", "select", "add", "delete", "clear", "enabled"):
                    return self._json({"ok": False, "error": "未知记忆操作"})
                api_rt_stop()
                VA.set_enabled(False)
                if action == "new": MEMORY.new_chat()
                elif action == "select": MEMORY.select(str(body.get("id", "")))
                elif action == "add": MEMORY.add_fact(body.get("text", ""))
                elif action == "delete": MEMORY.delete_fact(str(body.get("id", "")))
                elif action == "clear": MEMORY.clear()
                elif action == "enabled": MEMORY.set_setting("enabled", "1" if body.get("enabled") else "0")
                VA.history = MEMORY.history() if MEMORY.setting("enabled", "1") == "1" else []
                return self._json({"ok": True, **MEMORY.state()})
            if path == "/api/check":
                return self._json(api_check(body))
            if path == "/api/answer":
                return self._json(api_answer(body))
            if path == "/api/config":
                return self._json(api_config(body))
            if path == "/api/test":
                return self._json(api_test())
            if path == "/api/tts/test":
                return self._json(api_tts_test(body))
            if path == "/api/asr/test":
                return self._json(api_asr_test(body))
            if path == "/api/rt/start":
                return self._json(api_rt_start(body))
            if path == "/api/rt/stop":
                return self._json(api_rt_stop())
            if path == "/api/rt/say":
                return self._json(api_rt_say(body))
            if path == "/api/listen/start":
                api_rt_stop()          # 按住说话要独占麦克风
                return self._json(api_listen_start(body))
            if path == "/api/listen/stop":
                return self._json(api_listen_stop(body))
            if path == "/api/voice/wake":
                if body.get("enabled"):
                    api_rt_stop()      # 唤醒模式和实时通话互斥
                VA.set_enabled(bool(body.get("enabled")))
                return self._json({"ok": True, **VA.state()})
            if path == "/api/say":
                speak(body.get("text", ""))
                return self._json({"ok": True})
            if path == "/api/camera/restart":
                CAM.stop()
                time.sleep(0.5)
                CAM.start()
                return self._json({"ok": True})
            if path == "/api/camera/switch":
                return self._json(api_camera_switch(body))
        except ark.ProviderError as e:
            return self._json({"ok": False, "error": str(e)}, 200)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 200)
        return self._json({"ok": False, "error": "not found"}, 404)


def main():
    try:
        CAM.probe(with_modes=True)   # 启动时缓存一次分辨率列表，扫描按钮就不用再等
    except Exception:
        pass
    CAM.start()
    VA.start()
    if tts.available(CFG):
        threading.Thread(target=tts.warm, args=(CFG, [CFG.get("wake_reply") or "我在"]),
                         daemon=True).start()
    if CFG.get("wake_enabled"):
        VA.set_enabled(True)
    threading.Thread(target=hid_loop, daemon=True).start()
    port = int(CFG.get("port") or 8770)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"看书宝已启动： http://127.0.0.1:{port}")
    print(f"模型：{CFG.get('model')}　Key：{'已配置' if ark.api_key(CFG) else '未配置'}")

    def parent_watch():
        """App 退出后不留孤儿 python/ffmpeg 霸占摄像头。"""
        while True:
            time.sleep(2)
            if os.getppid() == 1:
                api_rt_stop()
                CAM.stop()
                os._exit(0)

    threading.Thread(target=parent_watch, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        api_rt_stop()
        CAM.stop()


if __name__ == "__main__":
    main()
