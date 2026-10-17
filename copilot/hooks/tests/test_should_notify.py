import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "script/should-notify.py"
STOP_INPUT = {"sessionId": "test-session", "timestamp": 123}
STOP = {
    "type": "hook.start",
    "data": {"hookType": "agentStop", "input": STOP_INPUT},
}
REPLY = {"type": "assistant.message", "data": {"content": "Done."}}


class ShouldNotifyTests(unittest.TestCase):
    def test_reply_classification(self):
        cases = [
            ("final reply", [REPLY, STOP], "true\n"),
            ("empty reply", [{"type": "assistant.message", "data": {}}, STOP], "false\n"),
            ("tool handoff", [
                {"type": "assistant.message", "data": {
                    "content": "Working.", "toolRequests": [{"name": "bash"}],
                }}, STOP,
            ], "false\n"),
            ("commentary", [
                {"type": "assistant.message", "data": {
                    "content": "Working.", "phase": "commentary",
                }}, STOP,
            ], "false\n"),
            ("turn boundary", [REPLY, {"type": "assistant.turn_start"}, STOP], "false\n"),
            ("subagent message", [
                REPLY, {"type": "assistant.message", "data": {"parentToolCallId": "tool"}}, STOP,
            ], "true\n"),
            ("encoded stop input", [REPLY, {
                "type": "hook.start",
                "data": {"hookType": "agentStop", "input": json.dumps(STOP_INPUT)},
            }], "true\n"),
            ("missing stop", [REPLY], None),
        ]
        with tempfile.TemporaryDirectory() as directory:
            transcript = Path(directory) / "events.jsonl"
            for name, events, expected in cases:
                with self.subTest(name=name):
                    transcript.write_text(
                        "".join(json.dumps(event) + "\n" for event in events),
                        encoding="utf-8",
                    )
                    result = subprocess.run(
                        [sys.executable, str(SCRIPT), str(transcript), "test-session", "123"],
                        cwd=directory, capture_output=True, text=True,
                    )
                    if expected is None:
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn("could not identify", result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, expected)


if __name__ == "__main__":
    unittest.main()
