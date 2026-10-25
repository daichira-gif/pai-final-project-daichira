#!/usr/bin/env python3

import argparse
import json
import math
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ALLOWED_ACTIONS = {"move", "rotate"}
MAX_STEPS = 8
POLL_INTERVAL = 0.05
SIGNAL_GRACE = 1.0


def valid_number(value, minimum, maximum):
    return (
        type(value) in (int, float)
        and math.isfinite(value)
        and minimum <= value <= maximum
    )


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise ValueError("Plan must be an object")

    if set(plan) != {"schema_version", "task_id", "steps"}:
        raise ValueError("Invalid plan fields")

    if type(plan["schema_version"]) is not int:
        raise ValueError("Invalid schema version")

    if plan["schema_version"] != 1:
        raise ValueError("Unsupported schema version")

    task_id = plan["task_id"]

    if not isinstance(task_id, str):
        raise ValueError("Invalid task ID")

    if not (
        1 <= len(task_id) <= 64
        and all(c.isascii() and (
            c.isalnum() or c in "_-"
        ) for c in task_id)
    ):
        raise ValueError("Invalid task ID")

    steps = plan["steps"]

    if not isinstance(steps, list):
        raise ValueError("Steps must be a list")

    if not 1 <= len(steps) <= MAX_STEPS:
        raise ValueError("Invalid step count")

    for index, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise ValueError(f"Step {index}: invalid object")

        action = step.get("action")

        if action not in ALLOWED_ACTIONS:
            raise ValueError(f"Step {index}: unknown action")

        if action == "move":
            if set(step) != {"action", "distance_m"}:
                raise ValueError(f"Step {index}: invalid fields")

            if not valid_number(
                step["distance_m"], 0.05, 0.5
            ):
                raise ValueError(f"Step {index}: invalid distance")

        if action == "rotate":
            if set(step) != {"action", "angle_deg"}:
                raise ValueError(f"Step {index}: invalid fields")

            angle = step["angle_deg"]

            if not (
                valid_number(angle, -180.0, 180.0)
                and abs(angle) >= 5.0
            ):
                raise ValueError(f"Step {index}: invalid angle")

    return steps


def build_command(step):
    if step["action"] == "move":
        return [
            "ros2", "run", "pai_final_control",
            "move_distance",
            "--ros-args",
            "-p", f'distance:={step["distance_m"]}',
            "-p", "speed:=0.04",
            "-p", "timeout:=35.0",
        ], 45

    return [
        "ros2", "run", "pai_final_control",
        "rotate_angle",
        "--ros-args",
        "-p", f'angle_deg:={float(step["angle_deg"])}',
        "-p", "max_speed:=0.30",
        "-p", "timeout:=35.0",
    ], 45


def log(message):
    print(message, flush=True)


class ShutdownRequest:
    """Handlers only record state; repeated signals cannot abort cleanup."""

    def __init__(self):
        self.signum = None
        self.count = 0
        self.previous = {}

    def handle(self, signum, frame):
        if self.signum is None:
            self.signum = signum
        self.count += 1

    def install(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.previous[sig] = signal.signal(sig, self.handle)

    def restore(self):
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)


def group_exists(process):
    """Conservative group-existence check, including unreaped zombies.

    Reap our direct child first. Descendants that leave this group are outside
    the scope. A group that cannot be confirmed absent blocks task success.
    """
    process.poll()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_group_exit(process, seconds):
    deadline = time.monotonic() + seconds
    while group_exists(process):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_INTERVAL)
    return True


def cleanup_process(process, label):
    """Bounded group shutdown, independent of cancellation flags."""
    ok = True
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
        if not group_exists(process):
            break
        log(f"PROCESS_SIGNAL {label}: PGID={process.pid} {sig.name}")
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log(f"PROCESS_CLEANUP_WARNING {label}: {exc}")
            ok = False
        if wait_group_exit(process, SIGNAL_GRACE):
            break

    if group_exists(process):
        log(f"PROCESS_RESIDUAL {label}: PGID={process.pid} PRESENT_OR_UNCONFIRMED")
        ok = False

    try:
        process.wait(timeout=SIGNAL_GRACE)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log(f"PROCESS_REAP_FAILED {label}: {exc}")
        ok = False

    if ok:
        log(f"PROCESS_CLEANUP_OK {label}: managed group absent; direct child reaped")
    return ok


def run_managed(command, timeout, request, label, honor_shutdown=True):
    """Return (reason, exit_code, cleanup_ok). No shell or JSON command hook."""
    if honor_shutdown and request.signum is not None:
        return "INTERRUPTED", None, True

    try:
        process = subprocess.Popen(command, start_new_session=True)
    except OSError as exc:
        log(f"PROCESS_START_FAILED {label}: {exc}")
        return "START_FAILED", None, True

    # Handler never raises, including between Popen and assignment.
    reason, returncode = "PROCESS_TIMEOUT", None
    cleanup_ok = False
    try:
        log(f"PROCESS_STARTED {label}: PID={process.pid} PGID={process.pid}")
        deadline = time.monotonic() + timeout
        while True:
            if honor_shutdown and request.signum is not None:
                reason = "INTERRUPTED"
                break
            returncode = process.poll()
            if returncode is not None:
                if group_exists(process):
                    reason = "RESIDUAL_PROCESS"
                else:
                    reason = "OK" if returncode == 0 else "NONZERO_EXIT"
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(POLL_INTERVAL)
    finally:
        cleanup_ok = cleanup_process(process, label)

    return reason, returncode, cleanup_ok


def stop_robot(request):
    command = [
        "ros2", "topic", "pub",
        "--times", "5", "--rate", "10",
        "--wait-matching-subscriptions", "1",
        "--keep-alive", "0.2",
        "/cmd_vel", "geometry_msgs/msg/Twist",
        "{linear: {x: 0.0, y: 0.0, z: 0.0}, "
        "angular: {x: 0.0, y: 0.0, z: 0.0}}",
    ]
    log("STOP_COMMAND_START")
    # A second SIGINT/SIGTERM must not cancel the bounded stop attempt.
    reason, code, cleaned = run_managed(
        command, 15.0, request, "STOP", honor_shutdown=False
    )
    ok = reason == "OK" and cleaned
    if ok:
        log("STOP_COMMAND_OK: publisher exited with code 0")
    else:
        log(f"STOP_WARNING: {reason} EXIT_CODE={code} CLEANUP_OK={cleaned}")
        log("STOP_COMMAND_FAILED")
    log("PHYSICAL_STOP_UNCONFIRMED: publish result is not a physical stop check")
    return ok


def pause_between_steps(request):
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if request.signum is not None:
            return False
        time.sleep(POLL_INTERVAL)
    return request.signum is None


def execute_steps(steps):
    request = ShutdownRequest()
    result_code = 1
    stop_ok = False
    request.install()
    try:
        log("EXECUTION_START")
        for i, step in enumerate(steps, 1):
            if request.signum is not None:
                break
            command, timeout = build_command(step)
            log(f"RUN_STEP {i}")
            reason, code, cleaned = run_managed(
                command, timeout, request, f"STEP_{i}"
            )
            if reason != "OK" or not cleaned:
                detail = f"EXIT_CODE={code}" if reason == "NONZERO_EXIT" else reason
                log(f"STEP_FAILED {i}: {detail} CLEANUP_OK={cleaned}")
                break
            log(f"STEP_SUCCESS {i}")
            if not pause_between_steps(request):
                break
        else:
            result_code = 0
    except Exception as exc:
        log(f"TASK_INTERRUPTED: {type(exc).__name__}: {exc}")
        result_code = 1
    finally:
        try:
            stop_ok = stop_robot(request)
        except Exception as exc:
            log(f"STOP_COMMAND_FAILED: {type(exc).__name__}: {exc}")
            log("PHYSICAL_STOP_UNCONFIRMED")
        finally:
            request.restore()

    if request.signum is not None:
        log(f"TASK_INTERRUPTED: {signal.Signals(request.signum).name} "
            f"SIGNAL_COUNT={request.count}")
        result_code = 128 + request.signum
    if not stop_ok and result_code == 0:
        result_code = 1
    log("TASK_SUCCESS" if result_code == 0 else f"TASK_FAILED: EXIT_CODE={result_code}")
    return result_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("plan")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Execute robot movement (default: dry run)",
    )
    args = parser.parse_args()

    try:
        plan = json.loads(
            Path(args.plan).read_text(encoding="utf-8")
        )
        steps = validate_plan(plan)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"PLAN_REJECTED: {exc}", flush=True)
        return 1

    print(
        f'PLAN_ACCEPTED: {plan["task_id"]}',
        flush=True
    )

    for i, step in enumerate(steps, 1):
        print(f"STEP {i}: {step}", flush=True)

    if not args.execute:
        print("DRY_RUN_PASS: no robot motion", flush=True)
        return 0

    return execute_steps(steps)


if __name__ == "__main__":
    sys.exit(main())
