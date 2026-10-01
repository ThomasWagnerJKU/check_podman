"""Tests for check_podman, with a fake podman (and sudo) replaying recorded ps/stats output."""
import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(os.path.dirname(HERE), "check_podman")
FIXTURES = os.path.join(HERE, "fixtures")

loader = importlib.machinery.SourceFileLoader("check_podman", PLUGIN)
spec = importlib.util.spec_from_loader("check_podman", loader)
check_podman = importlib.util.module_from_spec(spec)
loader.exec_module(check_podman)

# sudo -u USER PODMAN args...: drops "-u USER" and runs PODMAN directly
FAKE_SUDO = """#!/bin/sh
shift 2
exec "$@"
"""

# podman ps|stats ...: prints $FAKE_PS or $FAKE_STATS and logs its arguments
FAKE_PODMAN = """#!/bin/sh
echo "$*" >> "$FAKE_LOG"
case "$1" in
    ps) cat "$FAKE_PS" ;;
    stats) cat "$FAKE_STATS" ;;
esac
"""


def fixture(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return json.load(handle)


def args(**kwargs):
    values = dict(user="eda", name=[], warning=check_podman.Range.parse("80"), critical=check_podman.Range.parse("90"))
    values.update(kwargs)
    return type("Args", (), values)


class TestCheck(unittest.TestCase):
    """check() on recorded ps/stats output."""

    def check(self, ps, stats, **kwargs):
        state, message, perf = check_podman.check(args(**kwargs), fixture(ps), fixture(stats))
        self.assertNotIn("\n", message)
        return state, message, perf

    def test_ok(self):
        state, message, perf = self.check("ps_up.json", "stats_up.json")
        self.assertEqual(check_podman.OK, state, message)
        self.assertEqual("2 of 2 container(s) running: db, web", message)

    def test_perfdata(self):
        state, message, perf = self.check("ps_up.json", "stats_up.json")
        self.assertIn("'db_cpu'=0.41%", perf)
        self.assertIn("'db_mem'=214300000B;;;0;1000000000", perf)
        self.assertIn("'db_mem_percent'=20.93%;80;90", perf)
        self.assertIn("'db_net_in'=9698000B", perf)
        self.assertIn("'db_net_out'=24100000B", perf)
        self.assertIn("'db_block_in'=19970000B", perf)
        self.assertIn("'db_block_out'=60900000B", perf)
        self.assertIn("'db_pids'=12", perf)

    def test_container_down(self):
        state, message, perf = self.check("ps_mixed.json", "stats_web_only.json")
        self.assertEqual(check_podman.CRITICAL, state, message)
        self.assertEqual("db is down (Exited (1) 5 minutes ago), 1 of 2 container(s) running: web", message)
        # no stats for the down container, but the running one is still reported
        self.assertTrue(any(item.startswith("'web_cpu'") for item in perf), perf)

    def test_memory_warning(self):
        state, message, perf = self.check("ps_up.json", "stats_mem_high.json",
                                          critical=check_podman.Range.parse("95"))
        self.assertEqual(check_podman.WARNING, state, message)
        self.assertIn("web memory at 92.5%", message)

    def test_memory_critical(self):
        state, message, perf = self.check("ps_up.json", "stats_mem_high.json")
        self.assertEqual(check_podman.CRITICAL, state, message)
        self.assertIn("web memory at 92.5%", message)

    def test_memory_ok_below_threshold(self):
        state, message, perf = self.check("ps_up.json", "stats_up.json")
        self.assertEqual(check_podman.OK, state, message)

    def test_filter_by_name(self):
        state, message, perf = self.check("ps_up.json", "stats_up.json", name=["web"])
        self.assertEqual(check_podman.OK, state, message)
        self.assertEqual("1 of 1 container(s) running: web", message)
        self.assertFalse(any(item.startswith("'db_") for item in perf), perf)

    def test_name_not_found(self):
        with self.assertRaises(check_podman.PluginError) as caught:
            self.check("ps_up.json", "stats_up.json", name=["nope"])
        self.assertEqual(check_podman.CRITICAL, caught.exception.state)
        self.assertEqual("container(s) not found: nope", caught.exception.message)

    def test_no_containers(self):
        with self.assertRaises(check_podman.PluginError) as caught:
            check_podman.check(args(), [], [])
        self.assertEqual(check_podman.UNKNOWN, caught.exception.state)

    def test_parse_size(self):
        self.assertEqual(231700000.0, check_podman.parse_size("231.7MB"))
        self.assertEqual(2000000000.0, check_podman.parse_size("2GB"))
        self.assertEqual(1024.0, check_podman.parse_size("1KiB"))
        self.assertIsNone(check_podman.parse_size(None))
        self.assertIsNone(check_podman.parse_size("n/a"))

    def test_parse_percent(self):
        self.assertEqual(3.12, check_podman.parse_percent("3.12%"))
        self.assertEqual(0.0, check_podman.parse_percent("0.00%"))
        self.assertIsNone(check_podman.parse_percent(None))
        self.assertIsNone(check_podman.parse_percent("n/a"))

    def test_parse_pair(self):
        self.assertEqual(("4.738MB", "19.63MB"), check_podman.parse_pair("4.738MB / 19.63MB"))
        self.assertEqual((None, None), check_podman.parse_pair("nope"))
        self.assertEqual((None, None), check_podman.parse_pair(None))


class TestCommandLine(unittest.TestCase):
    """The plugin as Nagios runs it."""

    def setUp(self):
        self.tempdir = tempfile.mkdtemp()
        self.podman = self.executable("podman", FAKE_PODMAN)
        self.sudo = self.executable("sudo", FAKE_SUDO)
        self.log = os.path.join(self.tempdir, "log")
        open(self.log, "w").close()

    def tearDown(self):
        for name in os.listdir(self.tempdir):
            os.remove(os.path.join(self.tempdir, name))
        os.rmdir(self.tempdir)

    def executable(self, name, content):
        path = os.path.join(self.tempdir, name)
        with open(path, "w") as handle:
            handle.write(content)
        os.chmod(path, stat.S_IRWXU)
        return path

    def run_plugin(self, *argv, ps="ps_up.json", stats="stats_up.json", **env):
        environ = dict(os.environ, FAKE_PS=os.path.join(FIXTURES, ps), FAKE_STATS=os.path.join(FIXTURES, stats),
                       FAKE_LOG=self.log, PATH=self.tempdir + os.pathsep + os.environ["PATH"], **env)
        result = subprocess.run([sys.executable, PLUGIN, "-p", self.podman] + list(argv), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True, env=environ)
        return result

    def logged(self):
        with open(self.log) as handle:
            return handle.read().splitlines()

    def test_ok(self):
        result = self.run_plugin("-u", "eda")
        self.assertEqual(check_podman.OK, result.returncode, result)
        self.assertTrue(result.stdout.startswith("PODMAN OK - 2 of 2 container(s) running: db, web | "),
                        result.stdout)
        self.assertEqual(1, len(result.stdout.splitlines()))
        self.assertEqual(["ps -a --format json", "stats --no-stream --format json web db"], self.logged())

    def test_down(self):
        result = self.run_plugin("-u", "eda", ps="ps_mixed.json", stats="stats_web_only.json")
        self.assertEqual(check_podman.CRITICAL, result.returncode, result)
        self.assertTrue(result.stdout.startswith("PODMAN CRITICAL - db is down"), result.stdout)
        # stats is only queried for the container that is actually running
        self.assertEqual(["ps -a --format json", "stats --no-stream --format json web"], self.logged())

    def test_memory_threshold(self):
        result = self.run_plugin("-u", "eda", "-w", "80", "-c", "95", stats="stats_mem_high.json")
        self.assertEqual(check_podman.WARNING, result.returncode, result)
        self.assertIn("web memory at 92.5%", result.stdout)

    def test_no_containers_at_all(self):
        empty = os.path.join(self.tempdir, "empty.json")
        with open(empty, "w") as handle:
            handle.write("[]")
        result = self.run_plugin("-u", "eda", ps=os.path.relpath(empty, FIXTURES))
        self.assertEqual(check_podman.UNKNOWN, result.returncode, result)

    def test_sudo_rejected(self):
        sudo_denied = self.executable("sudo", "#!/bin/sh\necho 'sudo: a password is required' >&2\nexit 1\n")
        result = self.run_plugin("-u", "eda")
        self.assertEqual(check_podman.UNKNOWN, result.returncode, result)
        self.assertIn("a password is required", result.stdout)

    def test_usage_error_is_unknown(self):
        result = self.run_plugin()
        self.assertEqual(check_podman.UNKNOWN, result.returncode, result)
        result = self.run_plugin("-u", "eda", "-w", "abc")
        self.assertEqual(check_podman.UNKNOWN, result.returncode, result)
        self.assertEqual("PODMAN UNKNOWN - invalid threshold 'abc'\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
