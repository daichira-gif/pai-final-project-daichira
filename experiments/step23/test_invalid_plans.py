import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path("/workspace/PAI_final_project")
EXECUTOR = ROOT / "src/task_executor.py"

BASE_PLAN = {
    "schema_version": 1,
    "task_id": "test_invalid",
    "steps": [
        {"action": "move", "distance_m": 0.2}
    ],
}

TESTS = {
    "UNKNOWN_ACTION": {
        "action": "fly",
        "distance_m": 0.2,
    },
    "EXCESSIVE_DISTANCE": {
        "action": "move",
        "distance_m": 2.0,
    },
    "UNAUTHORIZED_SPEED": {
        "action": "move",
        "distance_m": 0.2,
        "speed": 0.5,
    },
}

if not EXECUTOR.is_file():
    print(f"ERROR: executor not found: {EXECUTOR}")
    sys.exit(2)

failed = 0

with tempfile.TemporaryDirectory() as tmp:
    for name, step in TESTS.items():
        plan = dict(BASE_PLAN)
        plan["steps"] = [step]
        path = Path(tmp) / f"{name}.json"
        path.write_text(
            json.dumps(plan, ensure_ascii=False),
            encoding="utf-8",
        )

        print(f"\n===== {name} =====", flush=True)

        try:
            result = subprocess.run(
                [sys.executable, str(EXECUTOR), str(path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print("FAIL: executor timeout")
            failed += 1
            continue

        print(result.stdout.strip())
        if result.stderr.strip():
            print("STDERR:", result.stderr.strip())

        print(f"EXIT_CODE={result.returncode}")

        rejected = (
            result.returncode != 0
            and "PLAN_REJECTED" in (
                result.stdout + result.stderr
            )
            and "PLAN_ACCEPTED" not in (
                result.stdout + result.stderr
            )
        )

        if rejected:
            print(f"PASS: {name}")
        else:
            print(f"FAIL: {name}")
            failed += 1

print("\n===== SUMMARY =====")
print(f"TOTAL={len(TESTS)}")
print(f"PASS={len(TESTS) - failed}")
print(f"FAIL={failed}")

sys.exit(0 if failed == 0 else 1)
