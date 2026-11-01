import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from memory import Memory


class MemoryTests(unittest.TestCase):
    def test_restart_delete_disable_and_chat_isolation(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]) as td:
            path = str(Path(td) / "memory.sqlite3")
            m = Memory(path)
            old = m.setting("chat")
            m.save(old, "记住：我在学六年级数学", "记住了")
            m = Memory(path)
            self.assertEqual(m.history()[0]["q"], "记住：我在学六年级数学")
            self.assertIn("六年级", m.fact_prompt())
            self.assertEqual(len(m.context()), 2)
            new = m.new_chat()
            self.assertEqual(m.history(), [])
            m.select(old)
            self.assertEqual(len(m.history()), 1)
            m.delete_fact(m.state()["facts"][0]["id"])
            self.assertNotIn("六年级", m.fact_prompt())
            self.assertEqual(m.context(), [])
            m.set_setting("enabled", "0")
            self.assertEqual(m.context(), [])
            m.save(old, "不应保存", "回答")
            self.assertEqual(len(m.history()), 0)
            m.clear()
            self.assertEqual(m.history(), [])
            self.assertEqual(m.state()["facts"], [])
            m.set_setting("enabled", "1")
            m.save(old, "过期回调", "不能恢复旧记录")
            self.assertEqual(m.history(old), [])


if __name__ == "__main__":
    unittest.main()
