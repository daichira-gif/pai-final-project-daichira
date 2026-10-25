import json
import subprocess
import sys
import tempfile
from pathlib import Path

EXECUTOR = Path(
    "/workspace/PAI_final_project/src/task_executor.py"
)


def plan(steps):
    return {
        "schema_version": 1,
        "task_id": "boundary_test",
        "steps": steps,
    }


def move(distance):
    return {"action": "move", "distance_m": distance}


def rotate(angle):
    return {"action": "rotate", "angle_deg": angle}


tests = [
    ("NEGATIVE_DISTANCE", plan([move(-0.5)]), False),
    ("ZERO_DISTANCE", plan([move(0)]), False),
    ("STRING_DISTANCE", plan([move("0.5")]), False),
    ("EMPTY_STEPS", plan([]), False),
    ("INVALID_JSON", '{"steps": [', False),
    ("NAN_DISTANCE", plan([move(float("nan"))]), False),
    ("INFINITY_DISTANCE", plan([move(float("inf"))]), False),
    ("BOOLEAN_DISTANCE", plan([move(True)]), False),
    ("TOO_MANY_STEPS", plan([move(0.1)] * 9), False),
    ("MIN_DISTANCE", plan([move(0.05)]), True),
    ("MAX_DISTANCE", plan([move(0.5)]), True),
    ("MIN_ROTATION", plan([rotate(5)]), True),
]

if not EXECUTOR.is_file():
    print(f"ERROR: executor missing: {EXECUTOR}")
    sys.exit(2)

failures = 0

with tempfile.TemporaryDirectory() as temp_dir:
    for name, data, should_accept in tests:
        file_path = Path(temp_dir) / f"{name}.json"

        if isinstance(data, str):
            content = data
        else:
            content = json.dumps(data, allow_nan=True)

        file_path.write_text(content, encoding="utf-8")

        print(f"\n===== {name} =====", flush=True)

        try:
            result = subprocess.run(
                [sys.executable, str(EXECUTOR), str(file_path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("FAIL: PROCESS_TIMEOUT")
            failures += 1
            continue

        output = result.stdout + result.stderr

        if should_accept:
            passed = (
                result.returncode == 0
                and "PLAN_ACCEPTED:" in output
                and "DRY_RUN_PASS:" in output
                and "PLAN_REJECTED:" not in output
            )
        else:
            passed = (
                result.returncode != 0
                and "PLAN_REJECTED:" in output
                and "PLAN_ACCEPTED:" not in output
                and "DRY_RUN_PASS:" not in output
            )

        print(output.strip())
        print(f"EXIT_CODE={result.returncode}")
        print(f"{'PASS' if passed else 'FAIL'}: {name}")

        if not passed:
            failures += 1

print("\n===== SUMMARY =====")
print(f"TOTAL={len(tests)}")
print(f"PASS={len(tests) - failures}")
print(f"FAIL={failures}")

sys.exit(0 if failures == 0 else 1)
