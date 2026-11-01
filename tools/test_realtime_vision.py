import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import realtime


class VisionTests(unittest.TestCase):
    def make_dialogue(self):
        rt = realtime.RealtimeDialogue({})
        rt.cam = Mock()
        rt.cam.snapshot.return_value = b"current-jpeg"
        rt._player = Mock()
        rt._send = Mock()
        return rt

    def test_frame_is_forwarded_and_audio_only_reply_is_suppressed(self):
        rt = self.make_dialogue()
        sent = threading.Event()
        rt._send.side_effect = lambda *args: sent.set()
        with patch.object(realtime.ark, "chat", return_value="第二题是计算题") as chat:
            rt._start_vision("第二题怎么做")
            rt._handle({"event": realtime.E_TTS_AUDIO, "raw": b"old-audio"})
            rt._player.push.assert_not_called()
            rt._handle({"event": realtime.E_CHAT_ENDED})
            rt._send.assert_not_called()
            rt._handle({"event": realtime.E_TTS_ENDED})
            self.assertTrue(sent.wait(2))
            self.assertEqual(chat.call_args.args[3], b"current-jpeg")
            self.assertIn("第二题是计算题", rt._send.call_args.args[1].decode())
            self.assertFalse(rt._vision_pending)

    def test_capture_failure_does_not_invent_visual_content(self):
        rt = self.make_dialogue()
        rt.cam.snapshot.side_effect = RuntimeError("camera offline")
        sent = threading.Event()
        rt._send.side_effect = lambda *args: sent.set()
        rt._start_vision("看摄像头")
        rt._handle({"event": realtime.E_CHAT_ENDED})
        rt._handle({"event": realtime.E_TTS_ENDED})
        self.assertTrue(sent.wait(2))
        self.assertIn("camera offline", rt.vision_error)
        self.assertIn("不要猜题", rt._send.call_args.args[1].decode())

    def test_stopped_call_drops_inflight_visual_result(self):
        rt = self.make_dialogue()
        entered, release = threading.Event(), threading.Event()

        def answer(*args, **kwargs):
            entered.set()
            release.wait(2)
            return "过期结果"

        with patch.object(realtime.ark, "chat", side_effect=answer):
            rt._start_vision("看这道题")
            self.assertTrue(entered.wait(2))
            rt.stop()
            release.set()
        rt._send.assert_not_called()

    def test_visual_routing_keeps_ordinary_chat_fast(self):
        self.assertTrue(realtime.RealtimeDialogue._needs_vision("第二题怎么做"))
        self.assertTrue(realtime.RealtimeDialogue._needs_vision("你能看到吗"))
        self.assertFalse(realtime.RealtimeDialogue._needs_vision("小豆小豆你好"))


if __name__ == "__main__":
    unittest.main()
