"""Service discovery tests. Systemd, /proc and the network are replaced by a JSON fixture (HCLUSTER_DISCOVERY_FIXTURE) or an injected
Scanner; everything runs in temp dirs, nothing touches the real host."""
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import redirect_stdout, redirect_stderr

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hcluster_agent import svcdiscover as sd
from hcluster_agent.config import load_config
from test_install import AGENT_DIR, FakeApi, Sandbox, mk_token

NODE = "11111111-1111-1111-1111-111111111111"
BASE = f'''# my notes: keep this comment
node:
  id: "{NODE}"
api:
  url: "https://x.example.com"
services: []

local_metrics:
  gpu: true   # trailing comment
'''

FX_H1 = {
    "systemd": [{"unit": "jellyfin.service", "description": "Jellyfin Media Server"},
                {"unit": "ssh.service", "description": "OpenBSD Secure Shell server"},
                {"unit": "docker.service", "description": "Docker Application Container Engine"},
                {"unit": "smbd.service", "description": "Samba SMB Daemon"}],
    "listeners": [{"port": 8096, "process": "jellyfin"}, {"port": 22, "process": "sshd"}, {"port": 22, "addr": "::", "process": "sshd"},
                  {"port": 445, "process": "smbd"}, {"port": 2283, "process": "docker-proxy"}, {"port": 3000, "process": "docker-proxy"},
                  {"port": 9090, "process": "cockpit-ws"}, {"port": 5432, "addr": "127.0.0.1", "process": "postgres"}, {"port": 41234}],
    "http": {"8096": {"/health": {"status": 200, "body": "Healthy"}},
             "2283": {"/api/server/ping": {"status": 200, "body": '{"res":"pong"}'}},
             "3000": {"/api/health": {"status": 200, "body": '{"database": "ok", "version": "11"}'}},
             "9090": {"/ping": {"status": 200, "scheme": "https", "body": '{"service":"cockpit"}'}}},
}


class Env:
    def __init__(self, fixture=None, conf=BASE):
        self.dir = tempfile.mkdtemp(prefix="hcs-disc-test-")
        self.conf = os.path.join(self.dir, "config.yaml")
        self.fx = os.path.join(self.dir, "fixture.json")
        with open(self.conf, "w") as fh:
            fh.write(conf)
        self.set_fixture(fixture or FX_H1)
        os.environ["HCLUSTER_DISCOVERY_FIXTURE"] = self.fx
        os.environ["HCLUSTER_SKIP_SYSTEMD"] = "1"

    def set_fixture(self, fx):
        with open(self.fx, "w") as fh:
            json.dump(fx, fh)

    def run(self, **kw):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = sd.run(self.conf, out=out, **kw)
        return rc, out.getvalue(), err.getvalue()

    def text(self):
        return open(self.conf).read()

    def services(self):
        return (yaml.safe_load(self.text()) or {}).get("services") or []

    def keys(self):
        return [s["key"] for s in self.services()]

    def backups(self):
        return sorted(f for f in os.listdir(self.dir) if ".bak-" in f)

    def close(self):
        os.environ.pop("HCLUSTER_DISCOVERY_FIXTURE", None)
        os.environ.pop("HCLUSTER_SKIP_SYSTEMD", None)
        shutil.rmtree(self.dir, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.e = Env()
        self.addCleanup(self.e.close)


class Detection(Base):
    def test_detects_common_services_with_evidence_and_valid_config(self):
        rc, out, _ = self.e.run()
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(self.e.keys()), ["cockpit", "docker", "grafana", "immich", "jellyfin", "samba", "ssh"])
        by = {s["key"]: s for s in self.e.services()}
        self.assertEqual(by["jellyfin"]["url"], "http://127.0.0.1:8096/health")
        self.assertEqual(by["immich"]["url"], "http://127.0.0.1:2283/api/server/ping")        # identified over HTTP although owned by docker-proxy
        self.assertEqual((by["ssh"]["type"], by["ssh"]["port"]), ("tcp", 22))
        self.assertEqual((by["samba"]["type"], by["samba"]["port"]), ("tcp", 445))
        self.assertEqual(by["docker"], {"key": "docker", "name": "Docker Engine", "type": "process", "process_name": "dockerd"})
        self.assertFalse(by["cockpit"]["verify_tls"])                                           # local self-signed https
        cfg = load_config(self.e.conf, {"HCLUSTER_TOKEN": "t"})                                 # the real agent config loader accepts it
        self.assertEqual(len(cfg.services), 7)
        self.assertEqual(self.e.text()[:self.e.text().index("services:")], BASE[:BASE.index("services:")])   # comments above untouched
        self.assertIn("# my notes: keep this comment", self.e.text())
        self.assertIn("gpu: true   # trailing comment", self.e.text())

    def test_unknown_ports_are_reported_but_never_named(self):
        fx = {"listeners": [{"port": 9090, "process": "mystery"}, {"port": 6000}, {"port": 40000}, {"port": 7000, "addr": "127.0.0.1"}],
              "http": {"6000": {"status": 200, "body": "hello"}}}
        self.e.set_fixture(fx)
        rc, out, _ = self.e.run()
        self.assertEqual(rc, 0)
        self.assertEqual(self.e.services(), [])                                                 # nothing added by default
        self.assertEqual(self.e.backups(), [])                                                  # ...and no pointless backup
        self.assertIn("port 9090", out)
        self.assertIn("conventionally used by", out)                                            # a hint, not a name
        self.assertIn("port 6000", out)
        self.assertNotIn("40000", out.split("skipped")[0])                                      # ephemeral: not even listed as unidentified
        self.assertIn("ephemeral", out)
        self.assertIn("loopback-only", out)
        rc, out, _ = self.e.run(include_unidentified=True)
        self.assertEqual(sorted(self.e.keys()), ["tcp-6000", "tcp-9090"])
        for s in self.e.services():
            self.assertTrue(s["name"].startswith("Unidentified TCP"), s)
            self.assertNotIn(s["name"].split()[0], ("Prometheus", "Cockpit", "Grafana"))
        self.assertEqual({s["key"]: s["type"] for s in self.e.services()}, {"tcp-6000": "http", "tcp-9090": "tcp"})

    def test_known_port_alone_is_not_enough_to_name_an_application(self):
        self.e.set_fixture({"listeners": [{"port": 8096}, {"port": 3000}, {"port": 22}], "http": {"3000": {"status": 200, "body": "totally different app"}}})
        self.e.run()
        self.assertEqual(self.e.services(), [])

    def test_duplicate_endpoints_collapse_to_one_check(self):
        fx = {"systemd": [{"unit": "pihole-FTL.service", "description": "Pi-hole FTL"}],
              "listeners": [{"port": 80, "process": "pihole-FTL"}, {"port": 80, "addr": "::", "process": "pihole-FTL"},
                            {"port": 8080, "process": "pihole-FTL"}, {"port": 53, "process": "pihole-FTL"}],
              "http": {"80": {"/admin/": {"status": 200, "body": "<title>Pi-hole</title>"}}, "8080": {"/admin/": {"status": 200, "body": "Pi-hole"}}}}
        self.e.set_fixture(fx)
        self.e.run()
        self.assertEqual(self.e.keys(), ["pihole"])                                             # one app = one check, not one per listener/port
        self.assertEqual(self.e.services()[0]["url"], "http://127.0.0.1:80/admin/")

    def test_minecraft_on_its_known_port_only_a_second_port_is_not_guessed(self):
        fx = {"systemd": [{"unit": "minecraft-survival.service", "description": "Minecraft survival"}],
              "listeners": [{"port": 25565, "process": "java"}, {"port": 25566, "process": "java"}]}
        self.e.set_fixture(fx)
        rc, out, _ = self.e.run()
        self.assertEqual(self.e.keys(), ["minecraft"])
        self.assertIn("port 25566", out)                                                        # reported as unidentified, not named

    def test_multi_instance_signature_gets_stable_port_suffixed_keys(self):
        with open(os.path.join(self.e.dir, "signatures.yaml"), "w") as fh:
            fh.write("signatures:\n  - id: game\n    name: Game\n    multi: true\n    units: ['^game']\n    ports: [7000, 7001]\n    http: {probe: false}\n    check: {type: tcp}\n")
        self.e.set_fixture({"systemd": [{"unit": "game.service", "description": ""}], "listeners": [{"port": 7001}, {"port": 7000}]})
        self.e.run()
        self.assertEqual(sorted(self.e.keys()), ["game", "game-7001"])
        keys_before = self.e.keys()
        self.e.set_fixture({"systemd": [{"unit": "game.service", "description": ""}], "listeners": [{"port": 7000}, {"port": 7001}]})    # order changes
        self.e.run()
        self.assertEqual(self.e.keys(), keys_before)

    def test_bind_address_is_used_when_not_wildcard(self):
        self.e.set_fixture({"listeners": [{"port": 22, "addr": "192.168.1.5", "process": "sshd"}]})
        self.e.run()
        self.assertEqual(self.e.services()[0]["host"], "192.168.1.5")

    def test_systemd_unit_without_listener_is_not_added_but_explained(self):
        self.e.set_fixture({"systemd": [{"unit": "jellyfin.service", "description": "x"}], "listeners": []})
        rc, out, _ = self.e.run()
        self.assertEqual(self.e.services(), [])
        self.assertIn("no listening port was found", out)

    def test_custom_signature_file_adds_and_overrides(self):
        with open(os.path.join(self.e.dir, "signatures.yaml"), "w") as fh:
            fh.write("signatures:\n  - id: myapp\n    name: My App\n    units: ['^myapp']\n    ports: [7777]\n    http: {probe: false}\n    check: {type: tcp}\n"
                     "  - id: ssh\n    name: Secure Shell\n    units: ['^ssh$']\n    processes: [sshd]\n    ports: [22]\n    http: {probe: false}\n    check: {type: tcp}\n")
        self.e.set_fixture({"systemd": [{"unit": "myapp.service", "description": ""}], "listeners": [{"port": 7777}, {"port": 22, "process": "sshd"}]})
        self.e.run()
        by = {s["key"]: s for s in self.e.services()}
        self.assertEqual(by["myapp"]["port"], 7777)
        self.assertEqual(by["ssh"]["name"], "Secure Shell")

    def test_bad_signature_file_is_rejected_without_touching_config(self):
        with open(os.path.join(self.e.dir, "signatures.yaml"), "w") as fh:
            fh.write("signatures:\n  - id: Bad Id\n")
        before = self.e.text()
        rc, _, err = self.e.run()
        self.assertEqual(rc, 2)
        self.assertEqual(self.e.text(), before)


class Idempotency(Base):
    def test_repeated_discovery_changes_nothing(self):
        self.e.run()
        first, n_backups = self.e.text(), len(self.e.backups())
        for _ in range(3):
            rc, out, _ = self.e.run()
            self.assertEqual(rc, 0)
            self.assertIn("config unchanged", out)
        self.assertEqual(self.e.text(), first)                                                  # byte-identical
        self.assertEqual(len(self.e.backups()), n_backups)                                      # no backup churn
        self.assertEqual(len(self.e.keys()), len(set(self.e.keys())))

    def test_newly_installed_service_is_picked_up_alone(self):
        self.e.run()
        before = self.e.services()
        fx = json.loads(json.dumps(FX_H1))
        fx["systemd"].append({"unit": "grafana-server.service", "description": "Grafana"})
        fx["listeners"].append({"port": 9091, "process": "docker-proxy"})
        fx["http"]["9091"] = {"/-/healthy": {"status": 200, "body": "Prometheus Server is Healthy."}}
        self.e.set_fixture(fx)
        rc, out, _ = self.e.run()
        after = self.e.services()
        self.assertEqual(after[:len(before)], before)                                           # old entries identical, in place
        self.assertEqual([s["key"] for s in after[len(before):]], ["prometheus"])
        self.assertEqual(after[-1]["url"], "http://127.0.0.1:9091/-/healthy")                   # the published host port, not an assumed 9090
        self.assertEqual(self.e.text().count(sd.BEGIN), 1)                                      # still ONE managed block
        self.assertEqual(len(self.e.backups()), 2)

    def test_stopped_service_is_reported_never_deleted(self):
        self.e.run()
        before = self.e.text()
        fx = json.loads(json.dumps(FX_H1))
        fx["systemd"] = [u for u in fx["systemd"] if "jellyfin" not in u["unit"]]
        fx["listeners"] = [l for l in fx["listeners"] if l["port"] != 8096]
        self.e.set_fixture(fx)
        rc, out, _ = self.e.run()
        self.assertEqual(rc, 0)
        self.assertIn("No longer detected", out)
        self.assertIn("jellyfin", out.split("No longer detected")[1])
        self.assertEqual(self.e.text(), before)                                                 # entry still there, file untouched
        rc, out, _ = self.e.run(as_json=True)
        self.assertEqual([m["key"] for m in json.loads(out)["not_detected"]], ["jellyfin"])


class ManualPreservation(unittest.TestCase):
    MANUAL = BASE.replace("services: []", '''services:
  # my hand-written checks
  - key: jellyfin
    name: "Media (custom name)"
    type: http
    url: "http://127.0.0.1:9999/custom"
    expected_status: [200, 204]
    interval_seconds: 15
    verify_tls: false
    retries: 3
  - key: web-ui                  # different key, but the SAME port as the discovered Grafana
    name: My dashboards
    type: http
    url: "http://localhost:3000/"
    timeout_seconds: 9

other_setting: kept''')

    def setUp(self):
        self.e = Env(conf=self.MANUAL)
        self.addCleanup(self.e.close)

    def test_manual_checks_are_never_overwritten_or_duplicated(self):
        before_raw = self.e.services()
        rc, out, _ = self.e.run()
        self.assertEqual(rc, 0, out)
        after = self.e.services()
        self.assertEqual(after[:2], before_raw)                                                 # custom name/url/interval/TLS/retries intact
        self.assertNotIn("grafana", self.e.keys())                                              # port 3000 is already covered by web-ui
        self.assertEqual([k for k in self.e.keys() if k == "jellyfin"], ["jellyfin"])
        self.assertIn("kept     jellyfin", out)
        self.assertIn("kept     grafana", out)
        t = self.e.text()
        self.assertTrue(t.startswith(self.MANUAL[:self.MANUAL.index("other_setting")].split("\n\nother_setting")[0][:200]))
        self.assertIn("# my hand-written checks", t)
        self.assertIn("other_setting: kept", t)                                                 # sections after services survive
        self.assertLess(t.index(sd.BEGIN), t.index("other_setting"))                            # managed block sits INSIDE services
        load_config(self.e.conf, {"HCLUSTER_TOKEN": "t"})

    def test_a_manual_process_check_prevents_a_duplicate_docker_check(self):
        e = Env(conf=BASE.replace("services: []", "services:\n  - key: engine\n    type: process\n    process_name: dockerd\n"))
        self.addCleanup(e.close)
        e.run()
        self.assertNotIn("docker", e.keys())


class SafetyAndRollback(Base):
    def test_dry_run_writes_nothing(self):
        before = self.e.text()
        rc, out, _ = self.e.run(dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIn("would add", out)
        self.assertEqual(self.e.text(), before)
        self.assertEqual(self.e.backups(), [])

    def test_backup_is_made_before_modification_and_matches_original(self):
        before = self.e.text()
        self.e.run()
        self.assertEqual(len(self.e.backups()), 1)
        self.assertEqual(open(os.path.join(self.e.dir, self.e.backups()[0])).read(), before)

    def test_file_mode_is_preserved(self):
        os.chmod(self.e.conf, 0o640)
        self.e.run()
        self.assertEqual(os.stat(self.e.conf).st_mode & 0o777, 0o640)
        self.assertEqual([f for f in os.listdir(self.e.dir) if f.startswith(".config.yaml.disc")], [])   # no temp litter

    def test_broken_existing_config_is_refused_untouched(self):
        with open(self.e.conf, "a") as fh:
            fh.write("\nservices:\n  - key: a\n   ? bad\n")
        before = self.e.text()
        rc, _, err = self.e.run()
        self.assertEqual(rc, 2)
        self.assertIn("nothing was changed", err)
        self.assertEqual(self.e.text(), before)
        self.assertEqual(self.e.backups(), [])

    def test_validation_failure_before_writing_leaves_config_untouched(self):
        orig = sd.merge_into_text
        sd.merge_into_text = lambda text, entries: text + "\n  : : broken ::\n"            # simulate a bug producing invalid YAML
        self.addCleanup(setattr, sd, "merge_into_text", orig)
        before = self.e.text()
        rc, _, err = self.e.run()
        self.assertEqual(rc, 1)
        self.assertIn("not valid YAML", err)
        self.assertEqual(self.e.text(), before)
        self.assertEqual(self.e.backups(), [])

    def test_a_change_that_would_alter_existing_entries_is_blocked(self):
        e = Env(conf=ManualPreservation.MANUAL)
        self.addCleanup(e.close)
        orig = sd.merge_into_text
        sd.merge_into_text = lambda text, entries: text.replace("interval_seconds: 15", "interval_seconds: 60")
        self.addCleanup(setattr, sd, "merge_into_text", orig)
        before = e.text()
        rc, _, err = e.run()
        self.assertEqual(rc, 1)
        self.assertIn("existing service entries would have changed", err)
        self.assertEqual(e.text(), before)

    def test_agent_that_fails_to_restart_triggers_rollback(self):
        calls = []
        before = self.e.text()
        orig = sd.make_restarter
        sd.make_restarter = lambda unit="x": (lambda: (calls.append(self.e.text() != before), len(calls) > 1)[1])   # first restart fails, second (after rollback) works
        self.addCleanup(setattr, sd, "make_restarter", orig)
        rc, _, err = self.e.run()
        self.assertEqual(rc, 1)
        self.assertIn("rolled back", err)
        self.assertEqual(self.e.text(), before)                                                 # previous config restored byte-for-byte
        self.assertEqual(calls, [True, False])                                                  # restart 1 saw the new file, restart 2 the restored one
        self.assertEqual(len(self.e.backups()), 1)                                              # backup kept for inspection

    def test_successful_restart_is_reported(self):
        orig = sd.make_restarter
        sd.make_restarter = lambda unit="x": (lambda: True)
        self.addCleanup(setattr, sd, "make_restarter", orig)
        rc, out, _ = self.e.run()
        self.assertEqual(rc, 0)
        self.assertIn("agent restarted", out)

    def test_no_restart_flag_never_restarts(self):
        orig = sd.make_restarter
        sd.make_restarter = lambda unit="x": (_ for _ in ()).throw(AssertionError("must not be called"))
        self.addCleanup(setattr, sd, "make_restarter", orig)
        self.assertEqual(self.e.run(no_restart=True)[0], 0)

    def test_unwritable_config_gives_a_clear_error(self):
        if os.geteuid() == 0:
            self.skipTest("root can write anywhere")
        os.chmod(self.e.dir, 0o500)
        self.addCleanup(os.chmod, self.e.dir, 0o700)
        rc, _, err = self.e.run()
        self.assertEqual(rc, 1)
        self.assertIn("sudo", err)

    def test_services_key_missing_or_in_the_middle(self):
        e = Env(conf=BASE.replace("services: []\n", ""))
        self.addCleanup(e.close)
        e.run()
        self.assertEqual(len(e.keys()), 7)
        e2 = Env(conf=BASE.replace("services: []", "# before\nservices:   # list\n\n# next section\nintervals:\n  heartbeat_seconds: 60\n"))
        self.addCleanup(e2.close)
        e2.run()
        t = e2.text()
        self.assertLess(t.index(sd.END), t.index("# next section"))
        self.assertIn("intervals:\n  heartbeat_seconds: 60", t)
        self.assertEqual(len(e2.keys()), 7)


class RealScannerAndHttp(unittest.TestCase):
    def test_real_proc_scan_sees_a_listening_socket_and_http_get_identifies_it(self):
        import http.server

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                b = b'{"database": "ok", "version": "11.0"}' if self.path == "/api/health" else b"nope"
                self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close); self.addCleanup(srv.shutdown)
        port = srv.server_address[1]
        sc = sd.Scanner()
        self.assertIn(port, [l.port for l in sc.listeners()])
        r = sc.http_get("http", "127.0.0.1", port, "/api/health")
        self.assertEqual(r["status"], 200)
        sig = next(s for s in sd.load_signatures() if s["id"] == "grafana")
        self.assertTrue(sd._http_matches(sig, r))
        self.assertIsNone(sc.http_get("http", "127.0.0.1", 9, "/"))                              # refused -> None, never raises

    def test_systemd_listing_parser_with_mocked_systemctl(self):
        class R:  # noqa: D401
            stdout = "jellyfin.service loaded active running Jellyfin Media Server\nssh.service loaded active running OpenBSD Secure Shell server\n"
        sc = sd.Scanner(run=lambda *a, **k: R())
        self.assertEqual(sc.systemd_units(), [("jellyfin", "Jellyfin Media Server"), ("ssh", "OpenBSD Secure Shell server")])
        sc2 = sd.Scanner(run=lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
        self.assertEqual(sc2.systemd_units(), [])


class InstallIntegration(unittest.TestCase):
    """Fresh install runs discovery; reinstall never touches an existing service list; the CLI works through the wrapper."""

    def setUp(self):
        self.api, self.sb = FakeApi(), Sandbox()
        self.node, self.tok = str(uuid.uuid4()), mk_token()
        self.api.tokens[self.tok] = self.node
        self.fx = os.path.join(self.sb.root, "fx.json")
        with open(self.fx, "w") as fh:
            json.dump(FX_H1, fh)
        self.extra = {"HCLUSTER_DISCOVERY_FIXTURE": self.fx}

    def tearDown(self):
        self.api.close()
        self.sb.cleanup()

    def test_fresh_install_discovers_and_monitors_services(self):
        r = self.sb.run("install.sh", "--api-url", self.api.url, "--node-id", self.node, "--yes", token=self.tok, extra=self.extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("discovering services", r.stdout)
        self.assertEqual(sorted(s["key"] for s in yaml.safe_load(self.sb.read(self.sb.config))["services"]),
                         ["cockpit", "docker", "grafana", "immich", "jellyfin", "samba", "ssh"])
        chk = subprocess.run([self.sb.bin_link, "check-config"], capture_output=True, text=True)
        self.assertIn("7 service check(s)", chk.stdout)
        self.assertEqual(sorted(os.listdir(self.sb.conf)).count("agent.env"), 1)
        self.assertEqual(self.sb.read(self.sb.envfile).strip(), f"HCLUSTER_TOKEN={self.tok}")

    def test_reinstall_does_not_rediscover_or_touch_the_service_list(self):
        self.sb.run("install.sh", "--api-url", self.api.url, "--node-id", self.node, "--yes", token=self.tok, extra=self.extra)
        with open(self.sb.config, "a") as fh:
            fh.write("\n# edited by hand\n")
        before = self.sb.snapshot()
        r = self.sb.run("install.sh", "--yes", extra=self.extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.sb.snapshot(), before)

    def test_no_discover_flag_and_failing_discovery_do_not_break_install(self):
        r = self.sb.run("install.sh", "--api-url", self.api.url, "--node-id", self.node, "--yes", "--no-discover", token=self.tok, extra=self.extra)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(yaml.safe_load(self.sb.read(self.sb.config)).get("services"), [])
        sb2 = Sandbox()
        try:
            bad = os.path.join(sb2.root, "bad.json")
            open(bad, "w").write("{not json")
            r2 = sb2.run("install.sh", "--api-url", self.api.url, "--node-id", self.node, "--yes", token=self.tok, extra={"HCLUSTER_DISCOVERY_FIXTURE": bad})
            self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)                            # discovery is best-effort
            self.assertIn("automatic discovery failed", r2.stderr)
        finally:
            sb2.cleanup()

    def test_discover_services_cli_after_install_is_idempotent_and_finds_new_apps(self):
        self.sb.run("install.sh", "--api-url", self.api.url, "--node-id", self.node, "--yes", "--no-discover", token=self.tok, extra=self.extra)
        env = {**os.environ, **self.extra, "HCLUSTER_SKIP_SYSTEMD": "1"}
        run = lambda *a: subprocess.run([self.sb.bin_link, "discover-services", *a], capture_output=True, text=True, env=env)  # noqa: E731
        r1 = run()
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        snap = self.sb.read(self.sb.config)
        r2 = run()
        self.assertEqual(self.sb.read(self.sb.config), snap)
        self.assertIn("config unchanged", r2.stdout)
        js = json.loads(run("--json").stdout)
        self.assertEqual(js["added"], [])
        self.assertEqual(len(js["already_monitored"]), 7)


if __name__ == "__main__":
    unittest.main()
