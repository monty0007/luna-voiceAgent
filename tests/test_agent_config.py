import os
import sys
import json
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir, "src"))

from create_agent import chunk_config, reassemble_config  # noqa: E402
import local_tools  # noqa: E402


class ChunkConfigTests(unittest.TestCase):
    def test_roundtrip_long_config(self):
        cfg = json.dumps({"session": {"x": "y" * 2000}})
        meta = chunk_config(cfg)
        self.assertTrue(all(len(v) <= 512 for v in meta.values()))
        self.assertEqual(reassemble_config(meta), cfg)

    def test_short_config_single_key(self):
        meta = chunk_config('{"a":1}')
        self.assertEqual(list(meta), ["microsoft.voice-live.configuration"])


class LocalToolTests(unittest.TestCase):
    def test_known_tool(self):
        out = json.loads(local_tools.run_tool("get_current_datetime", "{}"))
        self.assertIn("date", out)

    def test_unknown_tool_and_bad_args_return_errors(self):
        self.assertIn("error", json.loads(local_tools.run_tool("nope", "{}")))
        self.assertIn("error", json.loads(local_tools.run_tool("get_current_datetime", "{bad")))


if __name__ == "__main__":
    unittest.main()
