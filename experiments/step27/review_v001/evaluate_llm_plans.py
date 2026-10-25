#!/usr/bin/env python3
"""Eight dev cases, at most eight API calls, generation and dry-run only."""
import argparse
import contextlib
from datetime import datetime, timezone, timedelta
import getpass
import hashlib
import io
import json
import math
import os
from pathlib import Path
import runpy
import sys
import time
import uuid


MODEL = "nvidia/nemotron-3.5-super-vl:free"
CASES_SHA = "88274d6b6098f14e685c8eea0fc2d246b56e6f5e6c373670d7fc22823a93ec56"
PLANNER_SHA = "e1dde9798eacc9479fd5de96add7b101edaed43e74d973a5fc3f48c306235629"
EXECUTOR_SHA = "cae7c62d54dbc39d38f6f3236abe91999b3cff90c375c34065b724910160399b"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def steps_match(actual, expected):
    if not isinstance(actual, list) or len(actual) != len(expected):
        return False
    for got, want in zip(actual, expected):
        if not isinstance(got, dict) or set(got) != set(want):
            return False
        if got.get("action") != want["action"]:
            return False
        field = "distance_m" if want["action"] == "move" else "angle_deg"
        value = got.get(field)
        if type(value) not in (int, float) or not math.isfinite(value):
            return False
        if not math.isclose(value, want[field], rel_tol=0, abs_tol=1e-9):
            return False
    return True


class Tee(io.StringIO):
    def __init__(self, stream):
        super().__init__()
        self.stream = stream

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        return super().write(text)

    def flush(self):
        self.stream.flush()


def classify(case, exit_code, run_directory):
    record = json.loads((run_directory / "result.json").read_text(encoding="utf-8"))
    result = {"id": case["id"], "instruction": case["instruction"],
              "expected_status": case["expected_status"], "expected_steps": case["expected_steps"],
              "run_directory": str(run_directory), "exit_code": exit_code,
              "generation_status": record.get("status"), "usage": record.get("usage"),
              "generation_elapsed_seconds": record.get("elapsed_seconds")}
    if exit_code == 0 and record.get("status") == "PLAN_VALIDATED_DRY_RUN_PASS":
        actual = json.loads((run_directory / "plan.json").read_text(encoding="utf-8"))["steps"]
        result["actual_status"] = "accept"
        result["actual_steps"] = actual
        result["pass"] = case["expected_status"] == "accept" and steps_match(actual, case["expected_steps"])
        result["category"] = "CORRECT_ACCEPT" if result["pass"] else (
            "INCORRECT_ACCEPT" if case["expected_status"] == "reject" else "STEP_MISMATCH")
    elif exit_code == 3 and record.get("status") == "INSTRUCTION_REJECTED":
        result["actual_status"] = "reject"
        result["reason"] = record.get("error")
        result["pass"] = case["expected_status"] == "reject"
        result["category"] = "CORRECT_REJECT" if result["pass"] else "INCORRECT_REJECT"
    else:
        error = record.get("error", "")
        infrastructure = isinstance(error, str) and error.startswith(("HTTP_", "NETWORK_OR_TIMEOUT_ERROR", "API_ERROR_IN_RESPONSE", "API_RESPONSE_", "INVALID_API_", "INVALID_ASSISTANT_MESSAGE"))
        result.update(actual_status="error", error=error, **{"pass": False},
                      category="API_ERROR" if infrastructure else "GENERATION_OR_VALIDATION_ERROR")
    return result


def run_evaluation(project, cases, key, planner, output, pause=2.0):
    output.mkdir(parents=True, exist_ok=False)
    write = lambda p, obj: p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write(output / "cases.json", cases)
    results = []
    started = time.monotonic()
    for index, case in enumerate(cases):
        print(f'\nEVAL_CASE {index+1}/{len(cases)}: {case["id"]} {case["instruction"]}', flush=True)
        capture = Tee(sys.stdout)
        with contextlib.redirect_stdout(capture):
            code = planner["generate"](project, case["instruction"], key)
        text = capture.getvalue()
        (output / (case["id"] + ".log")).write_text(text, encoding="utf-8")
        paths = [line[len("RUN_DIRECTORY="):] for line in text.splitlines()
                 if line.startswith("RUN_DIRECTORY=")]
        if len(paths) != 1:
            raise RuntimeError("Could not identify unique generation run directory")
        directory = Path(paths[0]).resolve()
        if not directory.is_relative_to((project / "experiments/step26").resolve()):
            raise RuntimeError("Unexpected run directory")
        result = classify(case, code, directory)
        results.append(result)
        print(f'EVAL_{"PASS" if result["pass"] else "FAIL"}: {case["id"]} {result["category"]}', flush=True)
        write(output / "results_partial.json", results)
        if result["category"] == "API_ERROR" or code == 130:
            print("EVAL_STOPPED: no retries or remaining API calls after API error/interruption", flush=True)
            break
        if index + 1 < len(cases) and pause:
            time.sleep(pause)
    completed = len(results)
    passed = sum(r["pass"] for r in results)
    summary = {
        "model": MODEL, "suite": "DEV_EIGHT_CASES_V001",
        "planned_cases": len(cases), "completed_cases": completed,
        "not_run": [c["id"] for c in cases[completed:]],
        "pass": passed, "fail": completed - passed,
        "api_errors": sum(r["category"] == "API_ERROR" for r in results),
        "completed_case_pass_rate": passed / completed if completed else None,
        "normal": {"completed": sum(r["expected_status"] == "accept" for r in results),
                   "pass": sum(r["expected_status"] == "accept" and r["pass"] for r in results)},
        "rejection": {"completed": sum(r["expected_status"] == "reject" for r in results),
                      "pass": sum(r["expected_status"] == "reject" and r["pass"] for r in results)},
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "robot_motion_started": False, "client_retries": 0,
        "note": "Small visible dev set; N04 repeats the previous successful instruction. Not a held-out/general accuracy estimate.",
        "results": results,
    }
    write(output / "summary.json", summary)
    print(f'\nEVAL_SUMMARY: COMPLETED={completed}/{len(cases)} PASS={passed} FAIL={completed-passed} NOT_RUN={len(cases)-completed}', flush=True)
    print(f'EVAL_NORMAL: PASS={summary["normal"]["pass"]}/{summary["normal"]["completed"]}', flush=True)
    print(f'EVAL_REJECTION: PASS={summary["rejection"]["pass"]}/{summary["rejection"]["completed"]}', flush=True)
    print(f'API_ERRORS={summary["api_errors"]}', flush=True)
    print("SUMMARY_FILE=" + str(output / "summary.json"), flush=True)
    print("NO_ROBOT_MOTION: all generated plans remain unexecuted", flush=True)
    return 0 if completed == len(cases) and passed == len(cases) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, default=Path("/workspace/PAI_final_project"))
    parser.add_argument("--ask-key", action="store_true")
    args = parser.parse_args()
    project = args.project.resolve()
    source = project / "src/llm_plan_openrouter.py"
    if digest(source) != PLANNER_SHA or digest(project / "src/task_executor.py") != EXECUTOR_SHA:
        sys.exit("SOURCE_SHA_MISMATCH: no API calls started")
    planner = runpy.run_path(str(source))
    if planner["MODEL"] != MODEL:
        sys.exit("MODEL_MISMATCH: no API calls started")
    case_path = Path(__file__).with_name("cases.json")
    if digest(case_path) != CASES_SHA:
        sys.exit("CASE_FILE_SHA_MISMATCH: no API calls started")
    cases = json.loads(case_path.read_text(encoding="utf-8"))
    # Freeze this development set; altered files require explicit review.
    if len(cases) != 8 or [c.get("id") for c in cases] != ["N01", "N02", "N03", "N04", "R01", "R02", "R03", "R04"]:
        sys.exit("CASE_SET_MISMATCH")
    key = getpass.getpass("OpenRouter API key (hidden): ") if args.ask_key else os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        sys.exit("API_KEY_NOT_SET: use --ask-key")
    now = datetime.now(timezone(timedelta(hours=9)))
    output = project / "experiments/step27" / ("eval_" + now.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6])
    print("EVAL_DIRECTORY=" + str(output), flush=True)
    print("MAX_API_CALLS=8 / PINNED_FREE_MODEL / NO_ROBOT_MOTION", flush=True)
    return run_evaluation(project, cases, key, planner, output)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("EVAL_INTERRUPTED: inspect results_partial.json; no motion was launched", file=sys.stderr)
        sys.exit(130)
