#!/usr/bin/env python3
"""Linux isolated fake-ros2 tests. Never contacts ROS or the robot."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


FAKE_ROS = r'''#!PYTHON
import os, time
time.sleep(float(os.environ.get("FAKE_STARTUP_DELAY", "0")))
import json, os, signal, subprocess, sys, time
from pathlib import Path

def event(kind):
    with open(os.environ["FAKE_EVENTS"], "a") as f:
        f.write(json.dumps({"kind": kind, "pid": os.getpid()}) + "\n")

def ignore():
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)

def forever():
    while True:
        time.sleep(0.02)

if sys.argv[1:] == ["leaf"]:
    ignore()
    event("LEAF_READY")
    forever()

if sys.argv[1:3] == ["topic", "pub"]:
    event("STOP_READY")
    mode = os.environ.get("FAKE_STOP", "ok")
    if mode == "fail":
        sys.exit(9)
    if mode == "hang":
        ignore()
        forever()
    time.sleep(0.15)
    sys.exit(0)

if sys.argv[1:3] != ["run", "pai_final_control"]:
    sys.exit(98)

mode = os.environ.get("FAKE_MODE", "ok")
if mode == "stubborn":
    ignore()
if mode in ("tree", "orphan"):
    leaf = subprocess.Popen([sys.executable, __file__, "leaf"])
    deadline = time.monotonic() + 3
    while '"LEAF_READY"' not in Path(os.environ["FAKE_EVENTS"]).read_text():
        if time.monotonic() > deadline:
            sys.exit(97)
        time.sleep(.01)
event("MOVE_READY")
if mode == "orphan":
    sys.exit(0)
if mode == "fail":
    sys.exit(7)
if mode in ("timeout", "stubborn", "tree"):
    if mode == "tree":
        # The launcher dies on SIGINT; its leaf stays in the same group.
        signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    forever()
sys.exit(0)
'''


DRIVER = r'''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("executor_under_test", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
# Only the test driver changes time budgets; production still uses 45/5 sec.
original = m.build_command
def short_command(step):
    command, _ = original(step)
    return command, float(os.environ.get("FAKE_TIMEOUT", "4"))
m.build_command = short_command
original_run = m.run_managed
def short_stop(command, timeout, request, label, honor_shutdown=True):
    if label == "STOP":
        timeout = float(os.environ.get("FAKE_STOP_TIMEOUT", "5"))
    return original_run(command, timeout, request, label, honor_shutdown)
m.run_managed = short_stop
# Test-only handshake: exclude interpreter/import startup from deliberately
# short behavioral deadlines. Production Popen/deadlines remain untouched.
# A separate test bypasses this hook to verify startup counts in production.
real_popen = m.subprocess.Popen
def ready_popen(command, *args, **kwargs):
    child = real_popen(command, *args, **kwargs)
    if os.environ.get("FAKE_STARTUP_TEST") == "1":
        return child
    kind = "STOP_READY" if command[1:3] == ["topic", "pub"] else "MOVE_READY"
    deadline = m.time.monotonic() + 20
    try:
        while True:
            rows = m.Path(os.environ["FAKE_EVENTS"]).read_text().splitlines()
            for row in rows:
                try:
                    item = m.json.loads(row)
                except ValueError:
                    continue
                if item["pid"] == child.pid and item["kind"] == kind:
                    return child
            if child.poll() is not None:
                raise RuntimeError("FAKE_STARTUP_FAILED before readiness")
            if m.time.monotonic() >= deadline:
                raise RuntimeError("FAKE_STARTUP_TIMEOUT before readiness")
            m.time.sleep(.01)
    except BaseException:
        m.cleanup_process(child, "TEST_STARTUP")
        raise
m.subprocess.Popen = ready_popen
if os.environ.get("FAKE_CLEANUP_FAIL") == "1":
    original_cleanup = m.cleanup_process
    def cleanup_failed(process, label):
        cleaned = original_cleanup(process, label)
        return False if label == "STEP_1" else cleaned
    m.cleanup_process = cleanup_failed
if os.environ.get("FAKE_PRE_CANCEL") == "1":
    original_install = m.ShutdownRequest.install
    def pre_cancel(self):
        original_install(self)
        self.handle(m.signal.SIGTERM, None)
    m.ShutdownRequest.install = pre_cancel
sys.argv = [sys.argv[1]] + sys.argv[2:]
sys.exit(m.main())
'''


def live(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False



class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = self.root / "events.jsonl"
        self.events.touch()
        fake = self.root / "ros2"
        fake.write_text(FAKE_ROS.replace("#!PYTHON", "#!" + sys.executable, 1))
        fake.chmod(0o700)
        self.plan = self.root / "plan.json"
        self.plan.write_text(json.dumps({
            "schema_version": 1, "task_id": "process_test",
            "steps": [{"action": "move", "distance_m": .2},
                      {"action": "rotate", "angle_deg": 90}],
        }))
        self.driver = None
        self.output_file = self.root / "output.log"
        self.output_handle = self.output_file.open("w")

    def events_read(self):
        return [json.loads(s) for s in self.events.read_text().splitlines() if s]

    def start(self, mode="ok", stop="ok", timeout=4, execute=True, extra=None):
        env = os.environ.copy()
        # Deliberately exclude the system PATH so a fake-command failure can
        # never fall through to an installed real ros2 executable.
        env.update(PATH=str(self.root),
                   FAKE_EVENTS=str(self.events), FAKE_MODE=mode,
                   FAKE_STOP=stop, FAKE_TIMEOUT=str(timeout),
                   PYTHONUNBUFFERED="1")
        if extra:
            env.update(extra)
        args = [sys.executable, "-c", DRIVER, str(EXECUTOR), str(self.plan)]
        if execute:
            args.append("--execute")
        self.driver = subprocess.Popen(args, env=env, start_new_session=True,
                                       stdout=self.output_handle, stderr=subprocess.STDOUT)

    def reap_orphan_leaves(self):
        for item in self.events_read():
            if item["kind"] == "LEAF_READY":
                try:
                    os.waitpid(item["pid"], os.WNOHANG)
                except ChildProcessError:
                    pass

    def wait_for(self, token, events=False):
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            self.reap_orphan_leaves()
            text = self.events.read_text() if events else self.output_file.read_text()
            if token in text:
                return
            if self.driver.poll() is not None:
                self.fail(f"Driver exited before {token}: {self.output_file.read_text()}")
            time.sleep(.01)
        self.fail(f"No {token}: {self.output_file.read_text()}")

    def finish(self, success=False):
        deadline = time.monotonic() + 90
        while self.driver.poll() is None and time.monotonic() < deadline:
            self.reap_orphan_leaves()
            time.sleep(.01)
        code = self.driver.wait(timeout=.1)
        self.reap_orphan_leaves()
        out = self.output_file.read_text()
        self.assertEqual(code == 0, success, out)
        self.assertEqual("TASK_SUCCESS" in out, success, out)
        self.assertIn("PHYSICAL_STOP_UNCONFIRMED", out)
        if not success:
            self.assertIn("TASK_FAILED", out)
        for item in self.events_read():
            self.assertFalse(live(item["pid"]), f"Live PID {item}: {out}")
        return code, out

    def no_next_step(self, out):
        self.assertNotIn("RUN_STEP 2", out)

    def tearDown(self):
        # Harness-only subreaper below collects orphaned test grandchildren.
        # Production executor deliberately does not change process-wide reaping.
        if self.driver is not None and self.driver.poll() is None:
            self.driver.kill()
            self.driver.wait(timeout=3)
        for item in self.events_read():
            if live(item["pid"]):
                try:
                    os.kill(item["pid"], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                time.sleep(.01)
        self.output_handle.close()
        self.temp.cleanup()

    def test_normal_two_steps(self):
        self.start()
        _, out = self.finish(True)
        self.assertIn("STEP_SUCCESS 2", out)
        self.assertLess(out.index("STOP_COMMAND_OK"), out.index("TASK_SUCCESS"))

    def test_nonzero_blocks_next_step(self):
        self.start("fail")
        _, out = self.finish()
        self.assertIn("EXIT_CODE=7", out)
        self.no_next_step(out)

    def test_timeout(self):
        self.start("timeout", timeout=.4)
        _, out = self.finish()
        self.assertIn("PROCESS_TIMEOUT", out)
        self.no_next_step(out)

    def test_sigint(self):
        self.start("timeout")
        self.wait_for("MOVE_READY", True)
        self.driver.send_signal(signal.SIGINT)
        code, out = self.finish()
        self.assertEqual(code, 130, out)
        self.no_next_step(out)

    def test_sigterm(self):
        self.start("timeout")
        self.wait_for("MOVE_READY", True)
        self.driver.send_signal(signal.SIGTERM)
        code, out = self.finish()
        self.assertEqual(code, 143, out)
        self.no_next_step(out)

    def test_stubborn_requires_sigkill(self):
        self.start("stubborn", timeout=.4)
        _, out = self.finish()
        self.assertIn("SIGKILL", out)
        self.no_next_step(out)

    def test_descendant_cleanup(self):
        self.start("tree", timeout=.4)
        _, out = self.finish()
        self.assertIn("LEAF_READY", self.events.read_text())
        self.assertIn("SIGKILL", out)
        self.no_next_step(out)

    def test_leader_exits_with_live_descendant(self):
        self.start("orphan")
        _, out = self.finish()
        self.assertIn("RESIDUAL_PROCESS", out)
        self.assertIn("SIGKILL", out)
        self.no_next_step(out)

    def test_stop_nonzero_is_task_failure(self):
        self.start(stop="fail")
        _, out = self.finish()
        self.assertIn("STEP_SUCCESS 2", out)
        self.assertIn("STOP_COMMAND_FAILED", out)

    def test_stop_timeout_cleans_its_process(self):
        self.start(stop="hang", extra={"FAKE_STOP_TIMEOUT": ".7"})
        _, out = self.finish()
        self.assertIn("STOP_COMMAND_FAILED", out)
        self.assertIn("PROCESS_SIGNAL STOP", out)
        self.assertIn("SIGKILL", out)

    def test_signal_between_steps(self):
        self.start()
        self.wait_for("STEP_SUCCESS 1")
        self.driver.send_signal(signal.SIGTERM)
        code, out = self.finish()
        self.assertEqual(code, 143, out)
        self.no_next_step(out)

    def test_repeated_signal_during_cleanup(self):
        self.start("stubborn")
        self.wait_for("MOVE_READY", True)
        self.driver.send_signal(signal.SIGINT)
        self.wait_for("PROCESS_SIGNAL STEP_1")
        self.driver.send_signal(signal.SIGTERM)
        code, out = self.finish()
        self.assertEqual(code, 130, out)
        self.assertIn("SIGNAL_COUNT=2", out)
        self.assertIn("STOP_COMMAND_OK", out)

    def test_signal_during_stop(self):
        self.start()
        self.wait_for("STOP_READY", True)
        self.driver.send_signal(signal.SIGTERM)
        code, out = self.finish()
        self.assertEqual(code, 143, out)
        self.assertIn("STOP_COMMAND_OK", out)

    def test_dry_run_has_no_subprocess_side_effects(self):
        self.start(execute=False)
        self.assertEqual(self.driver.wait(timeout=3), 0)
        out = self.output_file.read_text()
        self.assertIn("DRY_RUN_PASS", out)
        self.assertEqual(self.events.read_text(), "")
        self.assertNotIn("EXECUTION_START", out)

    def test_invalid_execute_never_starts_process(self):
        self.plan.write_text('{"schema_version": 1}')
        self.start()
        self.assertNotEqual(self.driver.wait(timeout=3), 0)
        self.assertIn("PLAN_REJECTED", self.output_file.read_text())
        self.assertEqual(self.events.read_text(), "")

    def test_cleanup_failure_blocks_next_step(self):
        self.start(extra={"FAKE_CLEANUP_FAIL": "1"})
        _, out = self.finish()
        self.assertIn("CLEANUP_OK=False", out)
        self.no_next_step(out)

    def test_cancel_before_first_launch(self):
        self.start(extra={"FAKE_PRE_CANCEL": "1"})
        code, out = self.finish()
        self.assertEqual(code, 143, out)
        self.assertNotIn("RUN_STEP", out)
        self.assertNotIn("MOVE_READY", self.events.read_text())
        self.assertIn("STOP_COMMAND_OK", out)

    def test_slow_startup_does_not_consume_behavior_deadline(self):
        self.start(extra={"FAKE_STARTUP_DELAY": "1.2"})
        _, out = self.finish(True)
        self.assertIn("STEP_SUCCESS 2", out)

    def test_production_deadline_includes_startup(self):
        self.start(timeout=.4, extra={"FAKE_STARTUP_DELAY": "2",
                                      "FAKE_STARTUP_TEST": "1"})
        _, out = self.finish()
        self.assertIn("PROCESS_TIMEOUT", out)
        self.no_next_step(out)

    def test_missing_ros_executable_fails_safely(self):
        # Both launch and stop fail; there must be no false success.
        (self.root / "ros2").unlink()
        # Install an executable with a nonexistent interpreter; no real ROS fallback.
        (self.root / "ros2").write_text("#!/definitely/missing/python\n")
        (self.root / "ros2").chmod(0o700)
        self.start()
        _, out = self.finish()
        self.assertIn("PROCESS_START_FAILED", out)
        self.assertIn("STOP_COMMAND_FAILED", out)
        self.no_next_step(out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--executor", type=Path,
                        default=Path(__file__).resolve().parents[2] / "src/task_executor.py")
    args, rest = parser.parse_known_args()
    EXECUTOR = args.executor.resolve()
    if not sys.platform.startswith("linux"):
        sys.exit("Linux is required")
    # PR_SET_CHILD_SUBREAPER=36, solely in this isolated test runner.
    if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
        sys.exit("Test harness subreaper setup failed")
    unittest.main(argv=[sys.argv[0]] + rest, verbosity=2)
