"""Installer / updater / uninstaller tests.

Every test runs the REAL shell scripts inside its own temporary directory tree (INSTALL/CONF/STATE/UNIT/BIN paths are all
redirected) with HCLUSTER_SKIP_SYSTEMD=1, or, for the service-restart/rollback tests, with HCLUSTER_ALLOW_NONROOT=1 and a
stub `systemctl` that only appends to a log file. Nothing here can touch the real /etc, /opt, /var/lib, systemd, users or an
existing agent installation. A tiny fake Sentinel API answers the credential check like the real route does
(200 ok / 401 unknown token / 403 node_mismatch).
"""
import hashlib
import http.server
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, AGENT_DIR)
VENV_FLAGS = "--system-site-packages --without-pip"        # fast, offline: PyYAML comes from the system site-packages


def mk_token(cred=None):
    return f"hcs_{cred or uuid.uuid4()}_{secrets.token_urlsafe(32)[:43]}"


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class FakeApi:
    """Answers POST /api/agent/report like the real route: token decides the node."""

    def __init__(self):
        self.tokens: dict[str, str] = {}        # token -> node_id it belongs to
        self.requests: list[dict] = []
        self.force_status: int | None = None
        api = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                tok = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
                api.requests.append({"token": tok, "node_id": body.get("node_id")})
                if api.force_status:
                    return self._send(api.force_status, {"error": "forced"})
                if tok not in api.tokens:
                    return self._send(401, {"error": "unauthorized"})
                if api.tokens[tok] != body.get("node_id"):
                    return self._send(403, {"error": "node_mismatch"})
                return self._send(200, {"ok": True})

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class Sandbox:
    def __init__(self, src=AGENT_DIR):
        self.root = tempfile.mkdtemp(prefix="hcs-install-test-")
        self.src = src
        j = lambda *p: os.path.join(self.root, *p)  # noqa: E731
        self.install, self.conf, self.state = j("opt", "hcluster-agent"), j("etc", "hcluster-agent"), j("var", "lib", "hcluster-agent")
        self.unit_dir, self.bin_link, self.bindir = j("systemd"), j("bin", "hcluster-agent"), j("stubbin")
        self.systemctl_log = j("systemctl.log")
        for d in (self.unit_dir, os.path.dirname(self.bin_link), self.bindir):
            os.makedirs(d, exist_ok=True)

    # -- environment ------------------------------------------------------------------------------------------
    def env(self, token=None, systemd="skip", extra=None):
        e = {k: v for k, v in os.environ.items() if not k.startswith("HCLUSTER_")}
        e.update(HCLUSTER_INSTALL_DIR=self.install, HCLUSTER_CONF_DIR=self.conf, HCLUSTER_STATE_DIR=self.state,
                 HCLUSTER_UNIT_DIR=self.unit_dir, HCLUSTER_BIN_LINK=self.bin_link, HCLUSTER_VENV_FLAGS=VENV_FLAGS,
                 HOME=self.root, HCLUSTER_SETTLE_SECONDS="0", HCLUSTER_WAIT_SECONDS="3")
        if systemd == "skip":
            e["HCLUSTER_SKIP_SYSTEMD"] = "1"
        else:                                                    # stubbed systemctl, still no root / users / apt
            e["HCLUSTER_ALLOW_NONROOT"] = "1"
            e["HCLUSTER_SYSTEMCTL"] = os.path.join(self.bindir, "systemctl")
        if token:
            e["HCLUSTER_TOKEN"] = token
        e.update(extra or {})
        return e

    def run(self, script, *args, token=None, systemd="skip", extra=None, cwd=None):
        return subprocess.run(["bash", os.path.join(cwd or self.src, script), *args], env=self.env(token, systemd, extra),
                              capture_output=True, text=True, timeout=240)

    def install_sh(self, api, node, token, *extra_args, **kw):
        return self.run("install.sh", "--api-url", api, "--node-id", node, "--yes", "--no-discover", *extra_args, token=token, **kw)

    # -- helpers -------------------------------------------------------------------------------------------------
    def p(self, *parts):
        return os.path.join(self.root, *parts)

    @property
    def config(self):
        return os.path.join(self.conf, "config.yaml")

    @property
    def envfile(self):
        return os.path.join(self.conf, "agent.env")

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def snapshot(self):
        """Hash of every file under conf+state: proves 'nothing was changed'."""
        out = {}
        for base in (self.conf, self.state):
            for dp, _, fs in os.walk(base):
                for f in fs:
                    out[os.path.join(dp, f)] = sha(os.path.join(dp, f))
        return out

    def stub_systemctl(self, fail_first_restart=False):
        """A `systemctl` that logs its arguments; everything is 'active'. Optionally the FIRST restart fails."""
        path = os.path.join(self.bindir, "systemctl")
        with open(path, "w") as fh:
            fh.write(f'''#!/usr/bin/env bash
echo "$*" >> "{self.systemctl_log}"
case "$1" in
  restart)
    n=$(grep -c '^restart' "{self.systemctl_log}")
    if [[ "{1 if fail_first_restart else 0}" == "1" && "$n" == "1" ]]; then exit 1; fi
    exit 0;;
  is-active|is-enabled|daemon-reload|enable|disable|stop|status) exit 0;;
esac
exit 0
''')
        os.chmod(path, 0o755)

    def systemctl_calls(self):
        return self.read(self.systemctl_log).splitlines() if os.path.exists(self.systemctl_log) else []

    def app_version(self):
        py = os.path.join(self.install, "venv", "bin", "python")
        return subprocess.run([py, "-c", "import hcluster_agent as a; print(a.__version__)"], env={"PYTHONPATH": os.path.join(self.install, "app")},
                              capture_output=True, text=True, cwd=self.root).stdout.strip()

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def make_newer_source(sb, version="9.9.9", requirements_extra=""):
    """A copy of the agent source that claims a newer version (what `git pull` would bring)."""
    dst = os.path.join(sb.root, "src-new")
    shutil.copytree(AGENT_DIR, dst, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    init = os.path.join(dst, "hcluster_agent", "__init__.py")
    with open(init) as fh:
        text = fh.read()
    with open(init, "w") as fh:
        fh.write(text.replace('__version__ = "1.0.0"', f'__version__ = "{version}"'))
    if requirements_extra:
        with open(os.path.join(dst, "requirements.txt"), "a") as fh:
            fh.write(requirements_extra)
    return dst


class InstallerCase(unittest.TestCase):
    def setUp(self):
        self.api = FakeApi()
        self.sb = Sandbox()
        self.node_a, self.node_b = str(uuid.uuid4()), str(uuid.uuid4())
        self.tok_a, self.tok_b = mk_token(), mk_token()
        self.api.tokens[self.tok_a], self.api.tokens[self.tok_b] = self.node_a, self.node_b

    def tearDown(self):
        self.api.close()
        self.sb.cleanup()

    def fresh_a(self):
        r = self.sb.install_sh(self.api.url, self.node_a, self.tok_a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r


class FreshInstall(InstallerCase):
    def test_fresh_installation(self):
        r = self.fresh_a()
        sb = self.sb
        cfg = sb.read(sb.config)
        self.assertIn(self.node_a, cfg)
        self.assertIn(self.api.url, cfg)
        self.assertIn(f"state_dir: {sb.state}", cfg)                                     # custom state dir is wired in
        self.assertEqual(sb.read(sb.envfile).strip(), f"HCLUSTER_TOKEN={self.tok_a}")
        self.assertEqual(stat.S_IMODE(os.stat(sb.envfile).st_mode), 0o600)               # secret file is not world-readable
        self.assertTrue(os.access(os.path.join(sb.install, "venv", "bin", "python"), os.X_OK))
        self.assertTrue(os.path.isdir(os.path.join(sb.install, "app", "hcluster_agent")))
        self.assertTrue(os.access(sb.bin_link, os.X_OK))
        self.assertTrue(os.path.isdir(sb.state))
        layout = sb.read(os.path.join(sb.install, "layout.env"))
        self.assertIn(f"CONF_DIR={sb.conf}", layout)
        self.assertIn(f"STATE_DIR={sb.state}", layout)
        # the wrapper works and reads the token from agent.env
        out = subprocess.run([sb.bin_link, "check-config"], capture_output=True, text=True, env={**os.environ, "HCLUSTER_TOKEN": ""})
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn(self.node_a, out.stdout)
        # the credential pair was verified against the API (and only with this pair)
        self.assertEqual([(q["token"], q["node_id"]) for q in self.api.requests], [(self.tok_a, self.node_a)])
        self.assertNotIn(self.tok_a, r.stdout + r.stderr)                                # the secret is never echoed
        self.assertFalse(os.listdir(sb.install) and any(n.startswith(".app.new") for n in os.listdir(sb.install)))   # no staging litter

    def test_missing_token_is_an_error_not_a_prompt_with_yes(self):
        r = self.sb.run("install.sh", "--api-url", self.api.url, "--node-id", self.node_a, "--yes", "--no-discover")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HCLUSTER_TOKEN", r.stderr)

    def test_unreachable_api_warns_but_installs_unless_strict(self):
        dead = "http://127.0.0.1:9"                                                       # nothing listens here
        r = self.sb.install_sh(dead, self.node_a, self.tok_a)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("inconclusive", r.stderr)
        sb2 = Sandbox()
        try:
            r2 = sb2.install_sh(dead, self.node_a, self.tok_a, "--strict-verify")
            self.assertEqual(r2.returncode, 5, r2.stdout + r2.stderr)
            self.assertFalse(os.path.exists(sb2.config))                                  # nothing was written
        finally:
            sb2.cleanup()

    def test_unsafe_system_directory_is_refused(self):
        r = self.sb.run("install.sh", "--api-url", self.api.url, "--node-id", self.node_a, "--yes", token=self.tok_a,
                        extra={"HCLUSTER_CONF_DIR": "/etc"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("system directory", r.stderr)

    def test_mismatched_pair_on_a_fresh_install_is_rejected_before_writing(self):
        r = self.sb.install_sh(self.api.url, self.node_b, self.tok_a)                    # token of A, id of B
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("DIFFERENT node", r.stderr)
        self.assertFalse(os.path.exists(self.sb.config))
        self.assertFalse(os.path.exists(self.sb.envfile))


class Reinstall(InstallerCase):
    def test_repeat_with_same_node_id_preserves_everything(self):
        self.fresh_a()
        sb = self.sb
        with open(sb.config, "a") as fh:
            fh.write("\n# user edit that must survive\n")
        os.makedirs(sb.state, exist_ok=True)
        with open(os.path.join(sb.state, "queue.json"), "w") as fh:
            json.dump([{"collected_at": "2026-01-01T00:00:00Z"}], fh)
        before = sb.snapshot()
        r = sb.install_sh(self.api.url, self.node_a, self.tok_a)                          # same node id, same token
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sb.snapshot(), before)                                            # config, token, queue: byte-identical
        self.assertIn("kept existing", r.stdout)
        self.assertEqual(len(self.api.requests), 1)                                        # no needless second heartbeat

    def test_repeat_without_any_identity_options_is_a_safe_refresh(self):
        self.fresh_a()
        before = self.sb.snapshot()
        r = self.sb.run("install.sh", "--yes", "--no-discover")                            # no node id, no token needed
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sb.snapshot(), before)
        self.assertEqual(self.sb.read(self.sb.envfile).strip(), f"HCLUSTER_TOKEN={self.tok_a}")   # token preserved

    def test_different_node_id_is_refused_and_nothing_changes(self):
        self.fresh_a()
        before = self.sb.snapshot()
        r = self.sb.install_sh(self.api.url, self.node_b, self.tok_b)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("--reconfigure", r.stderr)
        self.assertIn(self.node_a, r.stderr)
        self.assertEqual(self.sb.snapshot(), before)

    def test_different_token_for_same_node_is_refused_without_reconfigure(self):
        self.fresh_a()
        self.api.tokens[(t2 := mk_token())] = self.node_a
        before = self.sb.snapshot()
        r = self.sb.install_sh(self.api.url, self.node_a, t2)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.sb.snapshot(), before)
        self.assertEqual(self.sb.read(self.sb.envfile).strip(), f"HCLUSTER_TOKEN={self.tok_a}")

    def test_different_api_url_is_refused_without_reconfigure(self):
        self.fresh_a()
        r = self.sb.install_sh("https://other.example.com", self.node_a, self.tok_a)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn(self.api.url, self.sb.read(self.sb.config))


class Reconfigure(InstallerCase):
    def test_reconfigure_to_another_node_backs_up_and_sets_the_old_queue_aside(self):
        self.fresh_a()
        sb = self.sb
        with open(sb.config, "a") as fh:
            fh.write("\nservices_note: kept\n")
        q = os.path.join(sb.state, "queue.json")
        with open(q, "w") as fh:
            json.dump([{"collected_at": "2026-01-01T00:00:00Z", "marker": "old-node"}], fh)
        old_cfg, old_env = sb.read(sb.config), sb.read(sb.envfile)
        r = sb.install_sh(self.api.url, self.node_b, self.tok_b, "--reconfigure")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(self.node_b, sb.read(sb.config))
        self.assertNotIn(self.node_a, sb.read(sb.config))
        self.assertIn("services_note: kept", sb.read(sb.config))                          # everything else in config is preserved
        self.assertEqual(sb.read(sb.envfile).strip(), f"HCLUSTER_TOKEN={self.tok_b}")
        backups = os.listdir(sb.conf)
        self.assertTrue(any(n.startswith("config.yaml.bak-") and sb.read(os.path.join(sb.conf, n)) == old_cfg for n in backups))
        self.assertTrue(any(n.startswith("agent.env.bak-") and sb.read(os.path.join(sb.conf, n)) == old_env for n in backups))
        aside = [n for n in os.listdir(sb.state) if n.startswith("queue.node-") and n.endswith(".json.bak")]
        self.assertEqual(len(aside), 1)                                                    # queued reports kept, not deleted ...
        self.assertIn("old-node", sb.read(os.path.join(sb.state, aside[0])))
        self.assertFalse(os.path.exists(q))                                                # ... and not replayed to the new node

    def test_reconfigure_rotates_the_token_of_the_same_node_and_keeps_the_queue(self):
        self.fresh_a()
        sb = self.sb
        self.api.tokens[(t2 := mk_token())] = self.node_a
        q = os.path.join(sb.state, "queue.json")
        with open(q, "w") as fh:
            json.dump([{"x": 1}], fh)
        r = sb.install_sh(self.api.url, self.node_a, t2, "--reconfigure")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sb.read(sb.envfile).strip(), f"HCLUSTER_TOKEN={t2}")
        self.assertTrue(os.path.exists(q))                                                 # same node: its queue is still valid

    def test_reconfigure_with_a_mismatched_pair_is_rejected_and_changes_nothing(self):
        self.fresh_a()
        before = self.sb.snapshot()
        r = self.sb.install_sh(self.api.url, self.node_b, self.tok_a, "--reconfigure")    # id of B with A's token
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.sb.snapshot(), before)

    def test_reconfigure_with_a_rejected_token_changes_nothing(self):
        self.fresh_a()
        before = self.sb.snapshot()
        r = self.sb.install_sh(self.api.url, self.node_b, mk_token(), "--reconfigure")    # token the API does not know
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("rejected", r.stderr)
        self.assertEqual(self.sb.snapshot(), before)

    def test_changing_node_id_requires_the_new_nodes_token(self):
        self.fresh_a()
        r = self.sb.run("install.sh", "--reconfigure", "--node-id", self.node_b, "--api-url", self.api.url, "--yes", "--no-discover")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("NEW node's token", r.stderr)


class ConfigValidation(InstallerCase):
    def break_config(self):
        with open(self.sb.config, "a") as fh:
            fh.write("\nservices:\n  - key: a\n   ? broken\n")

    def test_install_refresh_aborts_on_invalid_config_and_keeps_the_working_code(self):
        self.fresh_a()
        self.break_config()
        app_before = sha(os.path.join(self.sb.install, "app", "hcluster_agent", "__init__.py"))
        before = self.sb.snapshot()
        r = self.sb.run("install.sh", "--yes", "--no-discover")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not valid", r.stderr + r.stdout)
        self.assertEqual(self.sb.snapshot(), before)                                       # config + token untouched
        self.assertEqual(sha(os.path.join(self.sb.install, "app", "hcluster_agent", "__init__.py")), app_before)
        self.assertFalse(os.path.exists(os.path.join(self.sb.install, ".app.new")))

    def test_update_aborts_on_invalid_config_and_changes_nothing(self):
        self.fresh_a()
        self.break_config()
        newsrc = make_newer_source(self.sb)
        before = self.sb.snapshot()
        r = self.sb.run("update.sh", cwd=newsrc)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("aborted", r.stderr)
        self.assertEqual(self.sb.snapshot(), before)
        self.assertEqual(self.sb.app_version(), "1.0.0")                                   # the old code is still what is installed

    def test_check_config_reports_the_offending_lines(self):
        self.fresh_a()
        self.break_config()
        out = subprocess.run([self.sb.bin_link, "check-config"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)
        self.assertIn("near line", out.stderr)


class Update(InstallerCase):
    def test_update_keeps_identity_config_and_queue_untouched(self):
        self.fresh_a()
        sb = self.sb
        with open(sb.config, "a") as fh:
            fh.write("\n# node specific edit\n")
        with open(os.path.join(sb.state, "queue.json"), "w") as fh:
            json.dump([{"q": 1}], fh)
        before = sb.snapshot()
        newsrc = make_newer_source(sb)
        r = sb.run("update.sh", cwd=newsrc)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(sb.app_version(), "9.9.9")
        self.assertEqual(sb.snapshot(), before)
        self.assertTrue(os.path.isdir(os.path.join(sb.install, "app.prev")))               # old code kept for manual recovery
        self.assertFalse(os.path.exists(os.path.join(sb.install, "app.old")))
        self.assertEqual(len(self.api.requests), 1)                                        # updating sends no heartbeat of its own

    def test_update_finds_custom_paths_from_the_recorded_layout(self):
        self.fresh_a()
        newsrc = make_newer_source(self.sb)
        # only INSTALL_DIR is given: CONF/STATE/UNIT/BIN must come from layout.env, not from the defaults
        e = {k: v for k, v in self.sb.env().items() if k not in ("HCLUSTER_CONF_DIR", "HCLUSTER_STATE_DIR", "HCLUSTER_UNIT_DIR", "HCLUSTER_BIN_LINK")}
        r = subprocess.run(["bash", os.path.join(newsrc, "update.sh")], env=e, capture_output=True, text=True, timeout=240)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(self.sb.conf, r.stdout)
        self.assertEqual(self.sb.app_version(), "9.9.9")

    def test_unchanged_dependencies_reuse_the_virtualenv_changed_ones_build_a_new_one(self):
        self.fresh_a()
        marker = os.path.join(self.sb.install, "venv", "MARKER")
        open(marker, "w").write("old venv")
        r = self.sb.run("update.sh", cwd=make_newer_source(self.sb))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.exists(marker))                                            # same requirements: venv untouched
        sb2 = Sandbox()
        try:
            self.api.tokens[mk_token()] = "x"
            r0 = sb2.install_sh(self.api.url, self.node_a, self.tok_a)
            self.assertEqual(r0.returncode, 0, r0.stdout + r0.stderr)
            marker2 = os.path.join(sb2.install, "venv", "MARKER")
            open(marker2, "w").write("old venv")
            r2 = sb2.run("update.sh", cwd=make_newer_source(sb2, requirements_extra="\n# dependency set changed\n"))
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
            self.assertFalse(os.path.exists(marker2))                                      # a NEW venv was built and swapped in
            self.assertEqual(sb2.app_version(), "9.9.9")
            self.assertFalse(os.path.exists(os.path.join(sb2.install, "venv.old")))
        finally:
            sb2.cleanup()

    def test_install_without_layout_file_like_an_older_h1_install_still_updates(self):
        self.fresh_a()
        os.unlink(os.path.join(self.sb.install, "layout.env"))                              # installs made before layout.env existed
        r = self.sb.run("update.sh", cwd=make_newer_source(self.sb))                        # paths come from the (default-style) environment
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sb.app_version(), "9.9.9")
        self.assertIn(f"CONF_DIR={self.sb.conf}", self.sb.read(os.path.join(self.sb.install, "layout.env")))   # recorded from now on

    def test_update_refuses_when_not_installed(self):
        r = self.sb.run("update.sh")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("not installed", r.stderr)


class ServiceRestartAndRollback(InstallerCase):
    def test_install_and_update_with_systemd_use_the_recorded_custom_paths(self):
        self.sb.stub_systemctl()
        r = self.sb.install_sh(self.api.url, self.node_a, self.tok_a, systemd="stub")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        unit = self.sb.read(os.path.join(self.sb.unit_dir, "hcluster-agent.service"))
        for p in (self.sb.install, self.sb.conf, self.sb.state):
            self.assertIn(p, unit)                                                          # all three paths substituted
        self.assertNotIn("ReadWritePaths=/var/lib", unit)
        self.assertTrue(any(c.startswith("enable --now") for c in self.sb.systemctl_calls()))
        r2 = self.sb.run("update.sh", cwd=make_newer_source(self.sb), systemd="stub")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        unit2 = self.sb.read(os.path.join(self.sb.unit_dir, "hcluster-agent.service"))
        self.assertIn(self.sb.state, unit2)                                                 # update keeps the custom STATE_DIR (old bug)
        self.assertTrue(any(c.startswith("restart") for c in self.sb.systemctl_calls()))

    def test_failed_restart_rolls_back_to_the_previous_version_and_unit(self):
        self.sb.stub_systemctl()
        self.assertEqual(self.sb.install_sh(self.api.url, self.node_a, self.tok_a, systemd="stub").returncode, 0)
        unit_path = os.path.join(self.sb.unit_dir, "hcluster-agent.service")
        unit_before, before = self.sb.read(unit_path), self.sb.snapshot()
        self.sb.stub_systemctl(fail_first_restart=True)
        open(self.sb.systemctl_log, "w").close()
        newsrc = make_newer_source(self.sb)
        with open(os.path.join(newsrc, "systemd", "hcluster-agent.service"), "a") as fh:
            fh.write("\n# changed unit\n")
        r = self.sb.run("update.sh", cwd=newsrc, systemd="stub")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("rolling back", r.stderr)
        self.assertEqual(self.sb.app_version(), "1.0.0")                                    # previous code is back
        self.assertEqual(self.sb.read(unit_path), unit_before)                              # previous unit is back
        self.assertEqual(self.sb.snapshot(), before)                                        # identity/config/state never touched
        self.assertGreaterEqual(len([c for c in self.sb.systemctl_calls() if c.startswith("restart")]), 2)   # tried new, then restarted old
        self.assertFalse(os.path.exists(os.path.join(self.sb.install, "app.old")))


class Uninstall(InstallerCase):
    def populate(self):
        self.fresh_a()
        with open(os.path.join(self.sb.state, "queue.json"), "w") as fh:
            json.dump([{"q": 1}], fh)
        open(os.path.join(self.sb.conf, "config.yaml.bak-20260101-000000"), "w").write("old")

    def test_uninstall_without_purge_keeps_config_token_backups_and_queue(self):
        self.populate()
        sb = self.sb
        before = sb.snapshot()
        r = sb.run("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(sb.install))
        self.assertFalse(os.path.exists(sb.bin_link))
        self.assertEqual(sb.snapshot(), before)                                             # config, token, backup, queue all still there
        r2 = sb.install_sh(self.api.url, self.node_a, self.tok_a)                           # and reinstalling picks the node right up
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        self.assertIn("kept existing", r2.stdout)

    def test_uninstall_with_purge_removes_everything(self):
        self.populate()
        sb = self.sb
        r = sb.run("uninstall.sh", "--purge")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for path in (sb.install, sb.conf, sb.state, sb.bin_link):
            self.assertFalse(os.path.exists(path), path)

    def test_uninstall_removes_the_service_unit_when_systemd_is_used(self):
        self.sb.stub_systemctl()
        self.assertEqual(self.sb.install_sh(self.api.url, self.node_a, self.tok_a, systemd="stub").returncode, 0)
        unit = os.path.join(self.sb.unit_dir, "hcluster-agent.service")
        self.assertTrue(os.path.exists(unit))
        r = self.sb.run("uninstall.sh", systemd="stub")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(unit))
        self.assertTrue(any(c.startswith("disable --now") for c in self.sb.systemctl_calls()))

    def test_uninstall_refuses_a_system_directory(self):
        r = self.sb.run("uninstall.sh", "--purge", extra={"HCLUSTER_STATE_DIR": "/var/lib"})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("system directory", r.stderr)


class IdentModule(unittest.TestCase):
    def test_set_and_read_roundtrip_and_comments_survive(self):
        from hcluster_agent import ident
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "config.yaml")
            body = '# top comment\nnode:\n  id: "11111111-1111-1111-1111-111111111111"   # the id\n\napi:\n  url: "https://old.example.com"\n  timeout_seconds: 10\n\nservices:\n  - key: a   # id: not-a-node-id\n    name: A\n'
            with open(p, "w") as fh:
                fh.write(body)
            new_id = "22222222-2222-2222-2222-222222222222"
            self.assertTrue(ident.set_config_identity(p, new_id, "https://new.example.com"))
            self.assertEqual(ident.read_config_identity(p), (new_id, "https://new.example.com"))
            text = open(p).read()
            self.assertIn("# the id", text)
            self.assertIn("# top comment", text)
            self.assertIn("key: a   # id: not-a-node-id", text)                                     # lookalike line in services untouched
            self.assertFalse(ident.set_config_identity(p, new_id, "https://new.example.com"))       # idempotent
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_token_helpers(self):
        from hcluster_agent import ident
        cred = str(uuid.uuid4())
        tok = mk_token(cred)
        self.assertEqual(ident.token_credential_id(tok), cred)
        self.assertIsNone(ident.token_credential_id("nope"))
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "agent.env")
            open(p, "w").write(f"# c\nHCLUSTER_TOKEN=old\nHCLUSTER_TOKEN=\"{tok}\"\n")
            self.assertEqual(ident.read_env_token(p), tok)                                          # last assignment wins, quotes stripped
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
