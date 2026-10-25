#!/usr/bin/env python3
"""Offline evaluation tests using real planner validation, fake API replies."""
import argparse
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import runpy
import shutil
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("evaluator", Path(__file__).with_name("evaluate_llm_plans.py"))
evaluation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluation)
CASES = json.loads(Path(__file__).with_name("cases.json").read_text(encoding="utf-8"))


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "src").mkdir()
        shutil.copyfile(PROJECT / "src/task_executor.py", self.project / "src/task_executor.py")
        shutil.copyfile(PROJECT / "src/llm_plan_openrouter.py", self.project / "src/llm_plan_openrouter.py")
        self.planner = runpy.run_path(str(self.project / "src/llm_plan_openrouter.py"))
        self.calls = 0

    def tearDown(self):
        self.temp.cleanup()

    def evaluate(self, modify=None, error=None):
        def generate(project, instruction, key):
            case = next(c for c in CASES if c["instruction"] == instruction)
            self.calls += 1
            def transport(body, api_key):
                if error:
                    raise self.planner["PlanningError"](error)
                data = {"status": "ok" if case["expected_status"] == "accept" else "reject",
                        "reason": "" if case["expected_status"] == "accept" else "未対応です",
                        "steps": case["expected_steps"]}
                content = json.dumps(data)
                if modify:
                    content = modify(case, data, content)
                return {"model": evaluation.MODEL, "choices": [{"finish_reason": "stop",
                        "message": {"content": content}}]}
            return self.planner["generate"](project, instruction, key, transport=transport)
        output = self.project / "experiments/step27/test_eval"
        with contextlib.redirect_stdout(io.StringIO()):
            result = evaluation.run_evaluation(self.project, CASES, "sk-or-fake-key-for-tests", {"generate": generate}, output, pause=0)
        summary = json.loads((output / "summary.json").read_text())
        for path in self.project.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".txt", ".log"):
                self.assertNotIn("sk-or-fake-key-for-tests", path.read_text())
        self.assertFalse(summary["robot_motion_started"])
        return result, summary

    def test_all_expected_decisions_pass(self):
        code, summary = self.evaluate()
        self.assertEqual(code, 0)
        self.assertEqual(summary["pass"], 8)
        self.assertEqual(summary["normal"], {"completed": 4, "pass": 4})
        self.assertEqual(summary["rejection"], {"completed": 4, "pass": 4})
        self.assertEqual(self.calls, 8)

    def test_wrong_turn_direction_fails(self):
        def change(case, data, text):
            if case["id"] == "N02":
                data["steps"] = [{"action": "rotate", "angle_deg": 90}]
                return json.dumps(data)
            return text
        code, summary = self.evaluate(modify=change)
        self.assertEqual(code, 1)
        self.assertEqual(summary["fail"], 1)
        self.assertEqual(summary["results"][1]["category"], "STEP_MISMATCH")

    def test_out_of_scope_instruction_accepted_as_split_moves_fails(self):
        def change(case, data, text):
            if case["id"] == "R02":
                return json.dumps({"status": "ok", "reason": "", "steps": [
                    {"action": "move", "distance_m": .5}, {"action": "move", "distance_m": .5}]})
            return text
        code, summary = self.evaluate(modify=change)
        self.assertEqual(code, 1)
        self.assertEqual(summary["results"][5]["category"], "INCORRECT_ACCEPT")

    def test_false_rejection_fails(self):
        def change(case, data, text):
            return json.dumps({"status": "reject", "reason": "拒否", "steps": []}) if case["id"] == "N01" else text
        code, summary = self.evaluate(modify=change)
        self.assertEqual(code, 1)
        self.assertEqual(summary["results"][0]["category"], "INCORRECT_REJECT")

    def test_http_error_stops_remaining_calls(self):
        code, summary = self.evaluate(error="HTTP_404: Provider returned error")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(summary["api_errors"], 1)
        self.assertEqual(len(summary["not_run"]), 7)
        self.assertEqual(summary["results"][0]["category"], "API_ERROR")

    def test_http_200_error_envelope_classified_as_api_error(self):
        code, summary = self.evaluate(error="API_ERROR_IN_RESPONSE")
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, 1)
        self.assertEqual(summary["api_errors"], 1)

    def test_malformed_model_output_is_not_an_api_error(self):
        def change(case, data, text):
            return "not JSON" if case["id"] == "N01" else text
        code, summary = self.evaluate(modify=change)
        self.assertEqual(code, 1)
        self.assertEqual(self.calls, 8)
        self.assertEqual(summary["api_errors"], 0)
        self.assertEqual(summary["results"][0]["category"], "GENERATION_OR_VALIDATION_ERROR")

    def test_step_comparison_preserves_order_and_rejects_bool(self):
        expected = [{"action": "rotate", "angle_deg": 30}, {"action": "move", "distance_m": .1}]
        self.assertTrue(evaluation.steps_match(expected, expected))
        self.assertFalse(evaluation.steps_match(list(reversed(expected)), expected))
        self.assertFalse(evaluation.steps_match([{"action": "rotate", "angle_deg": True}], [{"action": "rotate", "angle_deg": 1}]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, default=Path("/workspace/PAI_final_project"))
    args, rest = parser.parse_known_args()
    PROJECT = args.project.resolve()
    unittest.main(argv=[__file__] + rest, verbosity=2)
