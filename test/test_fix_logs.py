import json
import tempfile
import unittest
from pathlib import Path

import fix_logs


class FixLogsTests(unittest.TestCase):
    def test_main_requires_email(self):
        with self.assertRaises(SystemExit):
            fix_logs.parse_args(["--log-dir", "test"])

    def test_main_fills_missing_email_from_explicit_argument(self):
        with tempfile.TemporaryDirectory(dir="test") as temp_dir:
            path = Path(temp_dir) / "rawchat_codex_2026-07-31.jsonl"
            path.write_text(
                json.dumps({"requestTime": "2026-07-31T10:00:00"}) + "\n",
                encoding="utf-8",
            )

            fix_logs.main(
                ["--log-dir", temp_dir, "--email", "test@example.com"]
            )

            record = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual("test@example.com", record["_account_email"])


if __name__ == "__main__":
    unittest.main()
