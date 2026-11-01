"""火山引擎「流式语音识别大模型」(SAUC) 客户端。

和现在「把整段 WAV 丢给多模态大模型转写」相比，这条路是边说边出字：
孩子说完约 1 秒就能拿到文字，省掉那段 3~6 秒的推理等待。

鉴权两种都支持：
  · 新版：X-Api-Key（语音控制台 → API Key 管理）
  · 旧版：X-Api-App-ID + X-Api-Access-Key（语音技术 → 应用管理）
注意：方舟（ark- 开头）的 Key 只负责大模型，不能用于语音识别。
"""
import gzip
import json
import struct
import threading
import time
import uuid

import ws

DEFAULT_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
DEFAULT_RESOURCE = "volc.bigasr.sauc.duration"

# ---- 二进制协议常量 ----
PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
AUDIO_ONLY_RESPONSE = 0b1011
SERVER_ERROR = 0b1111

NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
LAST_NO_SEQUENCE = 0b0010
NEG_SEQUENCE = 0b0011

NO_SERIAL = 0b0000
JSON_SERIAL = 0b0001

NO_COMPRESSION = 0b0000
GZIP = 0b0001


class ASRError(Exception):
    pass


def _header(msg_type: int, flags: int, serial: int = JSON_SERIAL, comp: int = GZIP) -> bytes:
    return bytes([
        (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE,
        (msg_type << 4) | flags,
        (serial << 4) | comp,
        0x00,
    ])


def _pack(payload: bytes, seq: int | None = None) -> bytes:
    body = gzip.compress(payload) if payload else b""
    out = b""
    if seq is not None:
        out += struct.pack(">i", seq)
    out += struct.pack(">I", len(body)) + body
    return out


def parse_response(data: bytes) -> dict:
    """拆一帧服务端响应，返回 {type, flags, seq, json|error}。"""
    if len(data) < 4:
        raise ASRError(f"响应太短：{data!r}")
    header_size = (data[0] & 0x0F) * 4 or 4
    msg_type = data[1] >> 4
    flags = data[1] & 0x0F
    serial = data[2] >> 4
    comp = data[2] & 0x0F
    off = header_size
    seq = None
    if flags in (POS_SEQUENCE, NEG_SEQUENCE):
        seq = struct.unpack(">i", data[off:off + 4])[0]
        off += 4
    if msg_type == SERVER_ERROR:
        code = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        size = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        body = data[off:off + size]
        if comp == GZIP and body:
            try:
                body = gzip.decompress(body)
            except OSError:
                pass
        raise ASRError(f"服务端错误 {code}：{body[:300].decode('utf-8', 'replace')}")
    size = struct.unpack(">I", data[off:off + 4])[0]
    off += 4
    body = data[off:off + size]
    if comp == GZIP and body:
        try:
            body = gzip.decompress(body)
        except OSError as e:
            raise ASRError(f"响应解压失败：{e}") from e
    out = {"type": msg_type, "flags": flags, "seq": seq, "raw": body}
    if serial == JSON_SERIAL and body:
        try:
            out["json"] = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            out["json"] = None
    return out


def credentials(cfg: dict) -> tuple[dict, str]:
    """返回（鉴权头，错误说明）。"""
    key = (cfg.get("asr_key") or "").strip()
    if key:
        return {"X-Api-Key": key}, ""
    appid = (cfg.get("asr_appid") or cfg.get("tts_appid") or "").strip()
    token = (cfg.get("asr_token") or cfg.get("tts_token") or "").strip()
    if appid and token:
        return {"X-Api-App-ID": appid, "X-Api-Access-Key": token}, ""
    return {}, ("还没配语音识别的凭证。需要「语音技术」控制台的 API Key，"
                "或 AppID + Access Token；方舟的 ark- Key 不能用于语音识别。")


def available(cfg: dict) -> bool:
    return bool(credentials(cfg)[0])


def _request_json(cfg: dict) -> bytes:
    req = {
        "model_name": "bigmodel",
        "enable_itn": True,
        "enable_punc": True,
        "enable_ddc": True,
        "show_utterances": True,
        "result_type": "single",
    }
    if cfg.get("asr_end_window_ms"):
        req["end_window_size"] = int(cfg["asr_end_window_ms"])
    payload = {
        "user": {"uid": str(cfg.get("asr_uid") or "kanshubao")[:32]},
        "audio": {"format": "pcm", "codec": "raw", "rate": 16000,
                  "bits": 16, "channel": 1},
        "request": req,
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class StreamASR:
    """一次识别会话：start → push(pcm)… → finish()。"""

    FRAME_BYTES = 3200          # 100 ms @ 16 kHz / 16 bit / 单声道

    def __init__(self, cfg: dict, on_partial=None):
        self.cfg = cfg
        self.on_partial = on_partial
        self.text = ""
        self.final_text = ""
        self.error = ""
        self.done = threading.Event()
        self.first_text_at: float | None = None
        self.started_at = 0.0
        self._ws = None
        self._buf = bytearray()
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = False
        self._reader = None

    # ---------- 生命周期 ----------
    def start(self):
        headers, err = credentials(self.cfg)
        if err:
            raise ASRError(err)
        url = (self.cfg.get("asr_url") or DEFAULT_URL).strip()
        headers = dict(headers)
        headers["X-Api-Resource-Id"] = (self.cfg.get("asr_resource_id") or DEFAULT_RESOURCE).strip()
        headers["X-Api-Request-Id"] = str(uuid.uuid4())
        headers["X-Api-Connect-Id"] = str(uuid.uuid4())
        headers["X-Api-Sequence"] = "-1"
        self._ws = ws.WebSocketClient(url, headers, timeout=20.0).connect()
        first = _header(FULL_CLIENT_REQUEST, NO_SEQUENCE) + _pack(_request_json(self.cfg))
        self._ws.send(first)
        self.started_at = time.time()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        return self

    def push(self, pcm: bytes):
        if not pcm or self._ws is None or self._stop:
            return
        with self._lock:
            self._buf += pcm
            while len(self._buf) >= self.FRAME_BYTES:
                chunk = bytes(self._buf[:self.FRAME_BYTES])
                del self._buf[:self.FRAME_BYTES]
                self._send_audio(chunk)

    def _send_audio(self, chunk: bytes, last: bool = False):
        if last:
            frame = _header(AUDIO_ONLY_REQUEST, LAST_NO_SEQUENCE, NO_SERIAL, GZIP) + _pack(b"")
            self._ws.send(frame)
            return
        self._seq += 1
        frame = _header(AUDIO_ONLY_REQUEST, POS_SEQUENCE, NO_SERIAL, GZIP) \
            + _pack(chunk, self._seq)
        self._ws.send(frame)

    def finish(self, timeout: float = 6.0) -> str:
        """送尾包，等最终文本。返回识别结果（可能为空）。"""
        with self._lock:
            rest = bytes(self._buf)
            self._buf.clear()
        try:
            if rest:
                self._send_audio(rest)
            self._send_audio(b"", last=True)
        except (ws.WSError, OSError) as e:
            self.error = self.error or f"发送尾包失败：{e}"
        deadline = time.time() + max(1.0, timeout)
        while time.time() < deadline and not self.done.is_set():
            time.sleep(0.02)
        text = (self.final_text or self.text).strip()
        return text

    def close(self):
        self._stop = True
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None

    # ---------- 收 ----------
    def _read_loop(self):
        try:
            while not self._stop:
                op, data = self._ws.recv(timeout=15.0)
                if op != ws.OP_BIN and op != ws.OP_TEXT:
                    continue
                try:
                    msg = parse_response(data)
                except ASRError as e:
                    self.error = str(e)
                    break
                if msg["type"] == AUDIO_ONLY_RESPONSE and not msg.get("json"):
                    continue
                obj = msg.get("json") or {}
                text = self._extract_text(obj)
                if text:
                    self.text = text
                    if self.first_text_at is None:
                        self.first_text_at = time.time()
                    if self.on_partial:
                        try:
                            self.on_partial(text)
                        except Exception:
                            pass
                if self._is_final(msg, obj):
                    self.final_text = text or self.text
                    self.done.set()
                    break
        except ws.WSError as e:
            if not self._stop:
                self.error = self.error or str(e)
        except Exception as e:      # noqa: BLE001 - 后台线程不能静默炸掉
            self.error = self.error or f"{type(e).__name__}: {e}"
        finally:
            self.done.set()

    @staticmethod
    def _extract_text(obj: dict) -> str:
        result = obj.get("result") or {}
        text = (result.get("text") or "").strip()
        if text:
            return text
        utts = result.get("utterances") or []
        return "".join((u.get("text") or "") for u in utts).strip()

    @staticmethod
    def _is_final(msg: dict, obj: dict) -> bool:
        if msg["flags"] in (NEG_SEQUENCE, LAST_NO_SEQUENCE):
            return True
        if obj.get("result", {}).get("final"):
            return True
        utts = (obj.get("result") or {}).get("utterances") or []
        return bool(utts) and all(u.get("definite") for u in utts) and bool(obj.get("result", {}).get("text"))


def transcribe_pcm(cfg: dict, pcm: bytes, timeout: float = 8.0) -> str:
    """一段 PCM 一次性识别（按住说话走这条，比大模型转写快很多）。"""
    session = StreamASR(cfg)
    session.start()
    try:
        session.push(pcm)
        return session.finish(timeout=timeout)
    finally:
        session.close()


def transcribe_wav_file(cfg: dict, path: str, timeout: float = 8.0) -> str:
    import wave
    with wave.open(path, "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ASRError(f"采样格式不支持：{w.getframerate()}Hz/{w.getnchannels()}ch/{w.getsampwidth()*8}bit")
        pcm = w.readframes(w.getnframes())
    return transcribe_pcm(cfg, pcm, timeout=timeout)
