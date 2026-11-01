"""小豆小豆：常听唤醒 + 自动录音问问题 + 朗读回答。"""
import os
import queue
import re
import tempfile
import threading
import time
import wave
from collections import deque

import ark
import asr
import mic
import prompts

WAKE_SYSTEM = "你只做唤醒词识别，不做别的判断。"
WAKE_PROMPT = "这段录音里有人叫“小豆”吗？有就只回答“是”，没有就只回答“否”。"


def _concat_wavs(paths: list[str], out_path: str) -> str:
    with wave.open(out_path, "wb") as out:
        first = True
        for p in paths:
            with wave.open(p, "rb") as w:
                if first:
                    out.setnchannels(w.getnchannels())
                    out.setsampwidth(w.getsampwidth())
                    out.setframerate(w.getframerate())
                    first = False
                out.writeframes(w.readframes(w.getnframes()))
    return out_path


class VoiceAssistant(threading.Thread):
    """后台线程：待唤醒 → 唤醒 → 听问题 → 回答。状态给前端轮询。"""

    def __init__(self, cfg: dict, snapshot, speak_blocking, busy_check=None, recorder=None):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.snapshot = snapshot
        self.speak_blocking = speak_blocking
        self.busy_check = busy_check or (lambda: False)
        self.enabled = False
        self.phase = "off"          # off / waiting / awake / listening / thinking / speaking / error
        self.detail = ""
        self.last_answer = ""
        self.last_at = 0.0
        self.last_timings = {}
        self.last_peak_db = -99.0
        self.last_rms_db = -99.0
        self.noise_db = -60.0
        self._rms_hist: deque = deque([-60.0] * 5, maxlen=15)
        self.history: list[dict] = []
        self.mic_device = ""
        self.partial = ""            # 流式识别正在出字
        self.asr_last_text = ""
        self.asr_last_ms = 0
        self.asr_error = ""
        self._pending_question = ""  # 识别好的问题，下一步直接用，不再上传音频
        self._stop = False
        self._paused = False
        self._hold = False
        self._q: queue.Queue | None = None
        self._phase_lock = threading.Lock()
        self._rec = recorder or mic.Recorder()
        self._stream: mic.StreamRecorder | None = None

    # ---------- 对外 ----------
    def set_enabled(self, on: bool):
        self.enabled = bool(on)
        self._set_phase("waiting" if on else "off")
        if not on and self._stream:
            self._stream.close()

    def hold_mic(self):
        """让出麦克风（按住说话要独占设备），并立刻停掉常开流。"""
        self._hold = True
        if self._stream:
            self._stream.close()

    def release_mic(self):
        self._hold = False

    def state(self) -> dict:
        return {"enabled": self.enabled, "phase": self.phase, "detail": self.detail,
                "last_answer": self.last_answer, "last_at": self.last_at,
                "last_timings": self.last_timings,
                "peak_db": round(self.last_peak_db, 1), "mic": self.mic_device,
                "rms_db": round(self.last_rms_db, 1), "noise_db": round(self.noise_db, 1),
                "threshold_db": round(self._voice_threshold(), 1),
                "partial": self.partial, "asr_ready": asr.available(self.cfg),
                "asr_last_text": self.asr_last_text, "asr_last_ms": self.asr_last_ms,
                "asr_error": self.asr_error[:160],
                "history": [dict(e) for e in self.history[-20:]]}

    def _add_entry(self, question: str = "") -> dict:
        entry = {"q": question, "a": "", "ts": time.time(),
                 "time": time.strftime("%H:%M"), "done": False}
        self.history.append(entry)
        del self.history[:-20]
        return entry

    def note_exchange(self, question: str, answer: str):
        """按住说话的问答也进同一份对话记录。"""
        entry = self._add_entry((question or "（语音提问）").strip())
        entry["a"] = (answer or "").strip()
        entry["done"] = True
        self.last_answer = entry["a"]
        self.last_at = time.time()
        callback = getattr(self, "on_round", None)
        if callback:
            callback(entry["q"], entry["a"])

    def _voice_threshold(self) -> float:
        """自适应人声门槛：比当前环境底噪高 6 dB，但不低于绝对下限。

        风扇、空调是持续底噪，孩子说话会明显高出这一截；用固定 dB 值
        在安静房间能用、在有风扇的房间就会一直误触发。
        """
        floor = min(self._rms_hist) if self._rms_hist else -60.0
        self.noise_db = floor
        return max(floor + 6.0, float(self.cfg.get("wake_min_db") or -48))

    def stop(self):
        self._stop = True

    # ---------- 内部 ----------
    def _set_phase(self, phase: str, detail: str = ""):
        with self._phase_lock:
            self.phase = phase
            self.detail = detail

    def _chunk(self, seconds: float = 1.0) -> str | None:
        try:
            self._rec.start(self.cfg.get("audio_hint") or "", int(seconds) + 1,
                            gain=float(self.cfg.get("mic_gain") or 1.0))
        except mic.MicError:
            return None
        time.sleep(seconds)
        try:
            st = self._rec.stop()
            return st["path"]
        except mic.MicError:
            return None

    def _heard_wake(self, path: str) -> bool:
        try:
            with open(path, "rb") as f:
                wav = f.read()
            reply = ark.chat(self.cfg, WAKE_SYSTEM, WAKE_PROMPT, None,
                             max_tokens=8, temperature=0, timeout=20,
                             audio_wav=wav, thinking="disabled")
        except ark.ProviderError as e:
            self._set_phase("error", str(e)[:120])
            time.sleep(2)
            return False
        return reply.strip().startswith("是") or "是" == reply.strip()[:1]

    def _collect_question(self, initial: list[str] | None = None,
                          wait_seconds: float | None = None,
                          detail: str = "听着呢，说吧") -> str | None:
        self._set_phase("listening", detail)
        silent, started = 0, False
        chunk_seconds = float(self.cfg.get("question_chunk_seconds") or 0.9)
        gain = float(self.cfg.get("mic_gain") or 1.0)
        silence_chunks = int(self.cfg.get("question_silence_chunks") or 2)
        max_chunks = int(self.cfg.get("question_max_chunks") or 9)
        wait_chunks = max(4, int((wait_seconds or 4.0) / max(0.4, chunk_seconds)))
        min_db = self._voice_threshold()
        pcm_all = bytearray()
        pre: deque = deque(maxlen=2)   # 人声开头前留两块，喂给识别做 VAD 铺垫
        session = None
        t_start = time.time()
        stream = mic.StreamRecorder()
        if self._stream:
            self._stream.close()
        self.partial = ""
        self._pending_question = ""
        try:
            stream.open(self.cfg.get("audio_hint") or "", chunk_seconds, gain)
            self.mic_device = (stream.device or {}).get("name", "")
            for i in range(max_chunks):
                pcm = stream.read_pcm()
                if not pcm:
                    break
                fd, tmp = tempfile.mkstemp(prefix="ksb_q_", suffix=".wav")
                os.close(fd)
                try:
                    mic.write_wav(tmp, pcm)
                    st = mic.wav_stats(tmp)
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                self.last_peak_db = float(st.get("peak_db") or -99)
                self.last_rms_db = float(st.get("rms_db") or -99)
                self._rms_hist.append(self.last_rms_db)
                if self.last_rms_db >= min_db:
                    started = True
                    silent = 0
                else:
                    silent = silent + 1 if started else 0
                if started:
                    if session is None:
                        session = self._open_asr(pre)
                        pre.clear()
                    if session:
                        session.push(pcm)
                    pcm_all += pcm
                else:
                    pre.append(pcm)
                if started and silent >= silence_chunks:   # 约 1.8 秒静音收工
                    break
                if not started and i >= wait_chunks:
                    break
        except mic.MicError:
            return None
        finally:
            stream.close()
        text = ""
        if session:
            try:
                text = session.finish(timeout=4.0)
            except Exception as e:            # noqa: BLE001 - 失败就回落到整段转写
                self.asr_error = f"{type(e).__name__}: {e}"[:160]
            finally:
                session.close()
            self.asr_last_ms = int((time.time() - t_start) * 1000)
        if text:
            self.asr_last_text = text
            self.partial = text
            self._pending_question = text
        else:
            self.partial = ""
        if not started or len(pcm_all) < 640:
            return None
        fd, out = tempfile.mkstemp(prefix="ksb_question_", suffix=".wav")
        os.close(fd)
        mic.write_wav(out, bytes(pcm_all))
        return out

    def _open_asr(self, preroll: deque):
        """开一路流式识别；没配凭证或连不上就返回 None，走原来的整段转写。"""
        if not asr.available(self.cfg):
            return None
        try:
            session = asr.StreamASR(self.cfg, on_partial=self._on_partial)
            session.start()
            for chunk in preroll:
                session.push(chunk)
            self.asr_error = ""
            return session
        except Exception as e:               # noqa: BLE001 - 识别不可用不能影响提问
            self.asr_error = f"{type(e).__name__}: {e}"[:160]
            return None

    def _on_partial(self, text: str):
        self.partial = text
        if self.phase == "listening":
            self._set_phase("listening", f"听到：{text}")

    @staticmethod
    def _cut_sentence(buf: str) -> tuple[str, str]:
        """从缓冲区切出可以立刻朗读的一句；长句在逗号处先断开。"""
        m = re.search(r"[。！？!?；;\n]+", buf)
        if m:
            return buf[:m.end()].strip(), buf[m.end():]
        if len(buf) >= 46:
            idx = max(buf.rfind("，"), buf.rfind(","), buf.rfind("："))
            if idx >= 16:
                return buf[:idx + 1].strip(), buf[idx + 1:]
        return "", buf

    def _answer(self, wav_path: str):
        self._set_phase("thinking", "小豆在想")
        try:
            with open(wav_path, "rb") as f:
                wav = f.read()
        finally:
            os.unlink(wav_path)
        pending = (self._pending_question or "").strip()
        self._pending_question = ""
        entry = self._add_entry(pending[:200])
        entry["a"] = ""

        if not pending:
            # 流式识别没接上：退回「整段音频丢给多模态模型转写」
            def transcribe():
                try:
                    system, user = prompts.transcribe_messages()
                    q = ark.chat(self.cfg, system, user, None, max_tokens=80,
                                 temperature=0, timeout=30, audio_wav=wav,
                                 thinking="disabled").strip().strip("“”\"'")
                    q = re.sub(r"^(小豆小豆|小豆)[，,。.！!？?\s]*", "", q).strip()
                    entry["q"] = (q or "（没听清）")[:200]
                except Exception:
                    entry["q"] = "（没听清）"
            threading.Thread(target=transcribe, daemon=True).start()
        img = None
        if self.cfg.get("voice_with_image", True):
            try:
                img = self.snapshot()
            except Exception:
                img = None
        if pending:
            system, user = prompts.voice_messages_text(self.cfg, pending)
            audio = None        # 已经有文字，不再上传音频，首句更快
        else:
            system, user = prompts.voice_messages(self.cfg)
            audio = wav
        memory_context = getattr(self, "memory_context", None)
        if memory_context:
            system += memory_context()
        sentences: queue.Queue = queue.Queue()
        full = {"text": "", "error": ""}

        def produce():
            buf = ""
            try:
                for piece in ark.chat_stream(self.cfg, system, user, img, max_tokens=220,
                                             temperature=0.3, timeout=90, audio_wav=audio,
                                             thinking=self.cfg.get("thinking") or "disabled"):
                    full["text"] += piece
                    buf += piece
                    entry["a"] = full["text"]
                    sentence, buf = self._cut_sentence(buf)
                    if sentence:
                        sentences.put(sentence)
                    if self.phase in ("thinking", "speaking"):
                        self.detail = full["text"]
                        self.last_answer = full["text"]
                if buf.strip():
                    sentences.put(buf.strip())
            except ark.ProviderError as e:
                full["error"] = str(e)
            except Exception as e:
                full["error"] = f"{type(e).__name__}: {e}"
            finally:
                sentences.put(None)

        threading.Thread(target=produce, daemon=True).start()
        spoke = False
        while not self._stop:
            sentence = sentences.get()
            if sentence is None:
                break
            if not spoke:
                self._set_phase("speaking", full["text"])
                spoke = True
            try:
                self.speak_blocking(sentence)
            except Exception:
                pass
        text = full["text"].strip()
        if not text:
            entry["a"] = "（这次没答上来）"
            entry["done"] = True
            self._set_phase("error", (full["error"] or "豆包没有返回内容")[:140])
            time.sleep(2)
            return
        self.last_answer = text
        self.last_at = time.time()
        entry["a"] = text
        entry["done"] = True
        callback = getattr(self, "on_round", None)
        if callback:
            callback(entry["q"], text)
        self._set_phase("waiting", text)

    def _record_loop(self):
        """常开麦克风、连续切块。每 1 秒一块，采音没有启停空档。

        只在“真需要独占麦克风”时（朗读回答、按住说话）才关流，
        平时一直保持打开，避免喊一句掉进重启间隙。
        """
        stream = mic.StreamRecorder()
        self._stream = stream
        while not self._stop:
            if not self.enabled or self._hold or self._paused or self.busy_check():
                stream.close()
                time.sleep(0.15)
                continue
            chunk_seconds = float(self.cfg.get("wake_chunk_seconds") or 1.0)
            gain = float(self.cfg.get("mic_gain") or 1.0)
            try:
                stream.open(self.cfg.get("audio_hint") or "", chunk_seconds, gain)
                self.mic_device = (stream.device or {}).get("name", "")
            except mic.MicError as e:
                self._set_phase("error", str(e)[:120])
                time.sleep(1.0)
                continue
            pcm = stream.read_pcm()
            if not pcm:
                stream.close()
                time.sleep(0.3)
                continue
            fd, path = tempfile.mkstemp(prefix="ksb_wake_", suffix=".wav")
            os.close(fd)
            try:
                mic.write_wav(path, pcm)
                st = mic.wav_stats(path)
            except Exception:
                st = {"peak_db": -99}
            self.last_peak_db = float(st.get("peak_db") or -99)
            self.last_rms_db = float(st.get("rms_db") or -99)
            self._rms_hist.append(self.last_rms_db)
            speech = self.last_rms_db >= self._voice_threshold()
            if self.phase == "error" and self.enabled and not self._paused:
                self._set_phase("waiting")
            if not speech:
                os.unlink(path)
                continue
            q = self._q
            if q is None:
                os.unlink(path)
                continue
            while q.full():
                try:
                    os.unlink(q.get_nowait())
                except Exception:
                    break
            q.put(path)

    def run(self):
        self._q = queue.Queue(maxsize=6)
        threading.Thread(target=self._record_loop, daemon=True).start()
        while not self._stop:
            if not self.enabled:
                self._drain()
                time.sleep(0.3)
                continue
            try:
                path = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if self._paused:
                os.unlink(path)
                continue
            try:
                t_wake = time.time()
                if not self._heard_wake(path):
                    continue
                t_detected = time.time()
                self._paused = True          # 交回麦克风，专心听问题
                if self._stream:
                    self._stream.close()
                self._drain()
                self._set_phase("awake", "我在")
                self.speak_blocking(self.cfg.get("wake_reply") or "我在")
                t_ack = time.time()
                # 唤醒那半秒只含“小豆小豆”，不并进问题录音；孩子听到“我在”后再说题
                qq = self._collect_question(initial=[])
                t_heard = time.time()
                turns = 0
                followups = max(0, int(self.cfg.get("followup_turns") or 0))
                while qq:
                    self._answer(qq)      # 思考与朗读期间保持暂停，免得录到自己的声音
                    turns += 1
                    plus = bool(self.cfg.get("followup_enabled", True))
                    if not plus or turns > followups or self._stop:
                        break
                    time.sleep(0.35)       # 等最后一句尾音散掉，避免自问自答
                    qq = self._collect_question(
                        initial=[],
                        wait_seconds=float(self.cfg.get("followup_seconds") or 8),
                        detail="继续问，或安静一会儿结束")
                if turns == 0:
                    self._set_phase("waiting", "我在，你问吧")
                t_done = time.time()
                self.last_timings = {
                    "wake": round(t_detected - t_wake, 2),
                    "ack": round(t_ack - t_detected, 2),
                    "listen": round(t_heard - t_ack, 2),
                    "answer": round(t_done - t_heard, 2),
                    "total": round(t_done - t_wake, 2),
                }
                self._paused = False
            finally:
                self._paused = False
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass

    def _drain(self):
        q = self._q
        if not q:
            return
        while True:
            try:
                os.unlink(q.get_nowait())
            except Exception:
                break
