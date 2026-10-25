#!/usr/bin/env python3
"""Offline tests: fake API responses + the actual pinned executor validator."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error


def response(content=None, finish="stop"):
    if content is None:
        content = json.dumps({"status": "ok", "reason": "", "steps": [
            {"action": "move", "distance_m": .1},
            {"action": "rotate", "angle_deg": 30},
            {"action": "move", "distance_m": .1}]})
    return {"id": "mock-response", "model": planner.MODEL, "provider": "mock",
            "usage": {"completion_tokens": 100},
            "choices": [{"finish_reason": finish,
                         "message": {"role": "assistant", "content": content}}]}


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "src").mkdir()
        self.executor = self.project / "src/task_executor.py"
        shutil.copyfile(EXECUTOR, self.executor)
        _, self.validator = planner.load_validator(self.project)
        self.key = "sk-or-test-not-a-real-key-123456789"
        self.capture = io.StringIO()

    def tearDown(self):
        self.temp.cleanup()

    def run_pipeline(self, payload=None, error=None):
        calls = []
        def transport(body, key):
            calls.append((body, key))
            if error:
                raise error
            return response() if payload is None else payload
        with contextlib.redirect_stdout(self.capture):
            code = planner.generate(self.project, "10cm前進して左に30度回転し10cm前進", self.key,
                                    transport=transport)
        self.assertEqual(len(calls), 1)
        directories = list((self.project / "experiments/step26").iterdir())
        self.assertEqual(len(directories), 1)
        directory = directories[0]
        for file in directory.iterdir():
            self.assertNotIn(self.key, file.read_text())
        self.assertNotIn(self.key, self.capture.getvalue())
        return code, directory, calls[0][0]

    def assert_no_plan(self, directory):
        self.assertFalse((directory / "plan.json").exists())
        self.assertFalse((directory / "candidate.json").exists())

    def test_success_uses_actual_validator_and_dry_run(self):
        calls = []
        real_run = subprocess.run
        def observe(command, **kwargs):
            calls.append((command, kwargs))
            return real_run(command, **kwargs)
        with mock.patch.object(planner.subprocess, "run", side_effect=observe):
            code, directory, body = self.run_pipeline()
        self.assertEqual(code, 0, self.capture.getvalue())
        self.assertTrue((directory / "plan.json").exists())
        self.assertIn("DRY_RUN_PASS", (directory / "dry_run.log").read_text())
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], [sys.executable, str(self.executor), str(directory / "candidate.json")])
        self.assertNotIn("--execute", calls[0][0])
        self.assertNotIn("OPENROUTER_API_KEY", calls[0][1]["env"])
        self.assertEqual(body["model"], "nvidia/nemotron-3.5-super-vl:free")
        self.assertFalse(body["provider"]["allow_fallbacks"])
        self.assertTrue(body["provider"]["require_parameters"])
        self.assertEqual(body["response_format"]["type"], "json_schema")
        plan = json.loads((directory / "plan.json").read_text())
        self.validator(plan)

    def test_model_rejection_never_runs_executor(self):
        payload = response(json.dumps({"status": "reject", "reason": "距離が不明です", "steps": []}))
        with mock.patch.object(planner.subprocess, "run", side_effect=AssertionError("must not run")):
            code, directory, _ = self.run_pipeline(payload)
        self.assertEqual(code, 3)
        self.assert_no_plan(directory)

    def test_nonzero_dry_run_blocks_plan(self):
        failed = subprocess.CompletedProcess([], 1, "PLAN_REJECTED: test", "")
        with mock.patch.object(planner.subprocess, "run", return_value=failed):
            code, directory, _ = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assert_no_plan(directory)

    def test_unexpected_execution_marker_blocks_plan(self):
        unexpected = subprocess.CompletedProcess([], 0,
            "PLAN_ACCEPTED: test\nDRY_RUN_PASS: test\nEXECUTION_START\n", "")
        with mock.patch.object(planner.subprocess, "run", return_value=unexpected):
            code, directory, _ = self.run_pipeline()
        self.assertEqual(code, 1)
        self.assert_no_plan(directory)

    def test_completion_length_is_rejected(self):
        code, directory, _ = self.run_pipeline(response(finish="length"))
        self.assertEqual(code, 1)
        self.assert_no_plan(directory)

    def test_api_failure_is_not_retried(self):
        code, directory, _ = self.run_pipeline(error=planner.PlanningError("HTTP_503: unavailable"))
        self.assertEqual(code, 1)
        self.assert_no_plan(directory)
        self.assertIn("HTTP_503", (directory / "result.json").read_text())

    def test_interrupt_has_no_accepted_plan(self):
        code, directory, _ = self.run_pipeline(error=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assert_no_plan(directory)
        self.assertIn("GENERATION_INTERRUPTED", (directory / "result.json").read_text())

    def test_key_is_redacted_from_error(self):
        code, directory, _ = self.run_pipeline(error=planner.PlanningError("bad key " + self.key))
        self.assertEqual(code, 1)

    def test_wrong_executor_hash_prevents_api(self):
        self.executor.write_text("# changed executor\n")
        transport = mock.Mock()
        with self.assertRaisesRegex(planner.PlanningError, "EXECUTOR_SHA_MISMATCH"):
            planner.generate(self.project, "10cm前進", self.key, transport=transport)
        transport.assert_not_called()

    def test_empty_instruction_prevents_api(self):
        transport = mock.Mock()
        with self.assertRaises(planner.PlanningError):
            planner.generate(self.project, "", self.key, transport=transport)
        transport.assert_not_called()

    def test_network_request_pinned_and_no_header_saved(self):
        raw = json.dumps(response()).encode()
        handle = mock.MagicMock()
        handle.__enter__.return_value.read.return_value = raw
        with mock.patch.object(planner.urllib.request, "urlopen", return_value=handle) as opener:
            data = planner.call_api(planner.request_body("10cm前進"), self.key)
        self.assertEqual(data["id"], "mock-response")
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, planner.API_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.key)
        self.assertEqual(opener.call_args.kwargs["timeout"], 180)

    def test_http_error_is_sanitized(self):
        error = urllib.error.HTTPError(planner.API_URL, 429, "rate limit", {},
            io.BytesIO(json.dumps({"error": {"message": "rate limit " + self.key}}).encode()))
        with mock.patch.object(planner.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(planner.PlanningError) as caught:
                planner.call_api(planner.request_body("10cm前進"), self.key)
        self.assertIn("HTTP_429", str(caught.exception))
        self.assertNotIn(self.key, str(caught.exception))

    def test_no_execute_cli_option(self):
        proc = subprocess.run([sys.executable, str(PLANNER_PATH), "--execute", "10cm前進"],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unrecognized arguments", proc.stderr)


def add_invalid_decision_test(name, content):
    def test(self):
        with mock.patch.object(planner.subprocess, "run", side_effect=AssertionError("must not run")):
            code, directory, _ = self.run_pipeline(response(content))
        self.assertEqual(code, 1, self.capture.getvalue())
        self.assert_no_plan(directory)
    setattr(PlannerTests, "test_reject_" + name, test)


def ok(steps):
    return json.dumps({"status": "ok", "reason": "", "steps": steps})


INVALID_CASES = {
    "markdown": '```json\n{"status":"ok","reason":"","steps":[]}\n```',
    "duplicate_key": '{"status":"reject","status":"ok","reason":"","steps":[]}',
    "bad_json": '{"status":',
    "extra_envelope_field": '{"status":"ok","reason":"","steps":[],"execute":true}',
    "unknown_action": ok([{"action": "shell", "command": "echo unsafe"}]),
    "excessive_distance": ok([{"action": "move", "distance_m": .6}]),
    "negative_distance": ok([{"action": "move", "distance_m": -.1}]),
    "boolean_distance": ok([{"action": "move", "distance_m": True}]),
    "string_distance": ok([{"action": "move", "distance_m": "0.1"}]),
    "nan": ok([{"action": "move", "distance_m": float("nan")}]),
    "overflow_float": '{"status":"ok","reason":"","steps":[{"action":"move","distance_m":1e309}]}',
    "speed_field": ok([{"action": "move", "distance_m": .1, "speed": 1}]),
    "zero_rotation": ok([{"action": "rotate", "angle_deg": 0}]),
    "excessive_rotation": ok([{"action": "rotate", "angle_deg": 181}]),
    "empty_steps": ok([]),
    "nine_steps": ok([{"action": "move", "distance_m": .1}] * 9),
    "reject_with_steps": '{"status":"reject","reason":"bad","steps":[{"action":"move","distance_m":0.1}]}',
}
for _name, _content in INVALID_CASES.items():
    add_invalid_decision_test(_name, _content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    project = Path(__file__).resolve().parents[2]
    parser.add_argument("--executor", type=Path, default=project / "src/task_executor.py")
    parser.add_argument("--planner", type=Path, default=project / "src/llm_plan_openrouter.py")
    args, remaining = parser.parse_known_args()
    EXECUTOR, PLANNER_PATH = args.executor.resolve(), args.planner.resolve()
    spec = importlib.util.spec_from_file_location("planner_under_test", PLANNER_PATH)
    planner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(planner)
    unittest.main(argv=[sys.argv[0]] + remaining, verbosity=2)
