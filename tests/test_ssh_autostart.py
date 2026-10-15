"""Exercise the embedded NAS worker without changing SSH or system services."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / "install-ssh-autostart-fix.sh"
MOCK = r'''#!/usr/bin/python3
import json, os, pathlib, sys
root = pathlib.Path(os.environ["SSH_TEST_ROOT"])
cfg = json.loads((root / "config.json").read_text())
now = int((root / "uptime").read_text().split()[0])
cmd, *args = sys.argv[1:]
with (root / "calls").open("a") as output:
    output.write(json.dumps([now, cmd, args]) + "\n")
if cmd == "sleep":
    later = now + int(args[0])
    (root / "uptime").write_text(f"{later} 0\n")
    if now < cfg.get("close_at", -1) <= later:
        (root / "active").unlink(missing_ok=True)
elif cmd == "logger":
    with (root / "logs").open("a") as output:
        output.write(" ".join(args) + "\n")
elif cmd == "systemctl":
    if args[0] == "show":
        finished = now >= cfg.get("ready_at", 0)
        rerun = cfg.get("rerun_start", -2) <= now < cfg.get("rerun_end", -1)
        if cfg.get("show_fails"):
            sys.exit(1)
        active, sub = ("active", "exited") if finished and not rerun else ("activating", "start")
        if cfg.get("failed") and finished:
            active, sub = "failed", "failed"
        start, end = (100, 200) if finished and not rerun else (100, 0)
        if cfg.get("never_ran"):
            active, sub, start, end = "inactive", "dead", 0, 0
        job = "99" if now < cfg.get("job_until", 0) else ""
        print(f"ActiveState={active}\nSubState={sub}\nExecMainStartTimestampMonotonic={start}\nExecMainExitTimestampMonotonic={end}\nResult=success\nJob={job}")
    elif args[0] == "is-active":
        sys.exit(0 if (root / "active").exists() else 3)
    elif args[0] == "start":
        if cfg.get("start_fails"):
            sys.exit(1)
        (root / "active").touch()
    else:
        sys.exit(2)
'''


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ssh-autostart-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mock = self.root / "mock"
        self.mock.write_text(MOCK)
        self.mock.chmod(0o755)
        source = INSTALLER.read_text()
        worker = source.split("<<'SSH_RESTORE_WORKER'\n", 1)[1].split("\nSSH_RESTORE_WORKER", 1)[0]
        worker = worker.replace("/usr/bin/systemctl", f"{self.mock} systemctl")
        worker = worker.replace("logger -t", f"{self.mock} logger -t")
        worker = worker.replace('sleep "$', f'{self.mock} sleep "$')
        worker = worker.replace("/proc/uptime", str(self.root / "uptime"))
        worker = worker.replace("/run/ssh-autostart-fix.lock", str(self.root / "lock"))
        self.worker = self.root / "worker.sh"
        self.worker.write_text(worker)
        self.env = {**os.environ, "SSH_TEST_ROOT": str(self.root)}

    def run_worker(self, *, probe=False, active=False, **cfg):
        (self.root / "config.json").write_text(json.dumps(cfg))
        (self.root / "uptime").write_text("0 0\n")
        if active:
            (self.root / "active").touch()
        return subprocess.run(["/bin/sh", str(self.worker)] + (["--check"] if probe else []),
                              env=self.env, capture_output=True, text=True, timeout=30)

    def calls(self, operation):
        path = self.root / "calls"
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [row for row in rows if row[1] == "systemctl" and row[2][0] == operation]

    def test_installer_and_worker_syntax(self):
        subprocess.run(["bash", "-n", str(INSTALLER)], check=True)
        subprocess.run(["/bin/sh", "-n", str(self.worker)], check=True)

    def test_wait_until_boot_finished(self):
        result = self.run_worker(ready_at=8)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in self.calls("start")], [8])
        self.assertEqual((self.root / "uptime").read_text(), "68 0\n")

    def test_pending_job_prevents_start(self):
        result = self.run_worker(job_until=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("start")[0][0], 10)

    def test_late_socket_stop_is_repaired(self):
        result = self.run_worker(active=True, close_at=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in self.calls("start")], [15])

    def test_second_boot_check_is_waited_for(self):
        result = self.run_worker(active=True, close_at=10, rerun_start=10, rerun_end=18)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[0] for call in self.calls("start")], [18])

    def test_active_socket_not_restarted(self):
        result = self.run_worker(active=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("start"), [])

    def test_failed_but_finished_check_allows_recovery(self):
        result = self.run_worker(failed=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.calls("start")), 1)

    def test_never_run_check_times_out_without_start(self):
        result = self.run_worker(never_ran=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.calls("start"), [])
        self.assertEqual((self.root / "uptime").read_text(), "300 0\n")

    def test_failed_state_read_times_out(self):
        result = self.run_worker(show_fails=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.calls("start"), [])

    def test_start_failures_are_bounded(self):
        result = self.run_worker(start_fails=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(len(self.calls("start")), 13)  # initial + 12 rechecks
        self.assertEqual((self.root / "uptime").read_text(), "60 0\n")

    def test_probe_does_not_mutate(self):
        result = self.run_worker(probe=True)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.calls("start"), [])
        self.assertFalse((self.root / "lock").exists())
        result = self.run_worker(probe=True, active=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_overlapping_worker_skips(self):
        with (self.root / "lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_worker()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls("show"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
