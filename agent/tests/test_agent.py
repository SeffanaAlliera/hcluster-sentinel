import os
import socket
import sys
import tempfile
import textwrap
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

from helpers import jresp, prom_empty, prom_vector, start_server  # noqa: E402
from hcluster_agent import config as C  # noqa: E402
from hcluster_agent.agent import Agent  # noqa: E402
from hcluster_agent.checks import run_check  # noqa: E402
from hcluster_agent.localmetrics import LocalCollector  # noqa: E402
from hcluster_agent.prometheus import collect, expand_selector  # noqa: E402
from hcluster_agent.reporter import Reporter  # noqa: E402

NODE = "11111111-2222-3333-4444-555555555555"


def mkcfg(**over) -> C.Config:
    base = dict(api_url="http://127.0.0.1:1", node_id=NODE, token="hcs_test", heartbeat_seconds=60, metrics_seconds=60,
                request_timeout_seconds=2, max_queue=50, state_dir=tempfile.mkdtemp(), local_fallback=True,
                net_exclude_prefixes=["lo"], disk_mountpoints=[], disk_exclude_prefixes=None, prometheus=C.PrometheusConfig(), services=[])
    base.update(over)
    return C.Config(**base)


def write_cfg(text: str) -> str:
    fd, p = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w") as f:
        f.write(textwrap.dedent(text))
    return p


class ConfigTests(unittest.TestCase):
    good = f"""
        node: {{id: "{NODE}"}}
        api: {{url: "https://sentinel.example.com"}}
        services:
          - {{key: jf, type: http, url: "http://127.0.0.1:8096/health"}}
          - {{key: smb, type: tcp, port: 445}}
        prometheus:
          enabled: true
          queries: [{{name: cpu_percent, expr: 'up'}}]
    """

    def test_valid(self):
        cfg = C.load_config(write_cfg(self.good), {"HCLUSTER_TOKEN": "tok"})
        self.assertEqual(cfg.token, "tok")
        self.assertEqual(len(cfg.services), 2)
        self.assertEqual(cfg.services[0].expected_status, [(200, 399)])
        self.assertEqual(cfg.services[1].host, "127.0.0.1")

    def test_token_required_and_never_from_yaml(self):
        with self.assertRaises(C.ConfigError):
            C.load_config(write_cfg(self.good), {})
        with self.assertRaises(C.ConfigError):
            C.load_config(write_cfg(self.good.replace("api: {", 'api: {token: "inyaml", ')), {})

    def test_token_file(self):
        tf = write_cfg("hcs_filetoken\n")
        cfg = C.load_config(write_cfg(self.good.replace("api: {", f'api: {{token_file: "{tf}", ')), {})
        self.assertEqual(cfg.token, "hcs_filetoken")

    def test_https_enforced(self):
        with self.assertRaises(C.ConfigError):
            C.load_config(write_cfg(self.good.replace("https://sentinel.example.com", "http://sentinel.example.com")), {"HCLUSTER_TOKEN": "t"})
        C.load_config(write_cfg(self.good.replace("https://sentinel.example.com", "http://127.0.0.1:3000")), {"HCLUSTER_TOKEN": "t"})

    def test_bad_values(self):
        for bad in (self.good.replace(NODE, "not-a-uuid"),
                    self.good.replace("key: jf", "key: JF"),
                    self.good.replace("key: smb", "key: jf"),
                    self.good.replace("type: tcp, port: 445", "type: tcp, port: 99999"),
                    self.good.replace("name: cpu_percent", "name: 'Bad Name'")):
            with self.assertRaises(C.ConfigError, msg=bad):
                C.load_config(write_cfg(bad), {"HCLUSTER_TOKEN": "t"})

    def test_status_ranges(self):
        self.assertEqual(C.parse_status_ranges([200, "301-302"]), [(200, 200), (301, 302)])
        with self.assertRaises(C.ConfigError):
            C.parse_status_ranges("2xx")

    def test_example_config_parses(self):
        d = os.path.join(os.path.dirname(__file__), "..")
        load = lambda f: C.load_config(write_cfg(open(os.path.join(d, f)).read().replace("00000000-0000-0000-0000-000000000000", NODE)), {"HCLUSTER_TOKEN": "t"})
        generic = load("config.example.yaml")
        self.assertEqual(generic.services, [])                       # new nodes start with NO assumed services
        self.assertGreaterEqual(len(generic.prometheus.queries), 10)
        h1 = load("config.h1.example.yaml")
        self.assertEqual({s.key for s in h1.services}, {"jellyfin", "immich", "pihole", "syncthing", "samba", "portainer", "gptwol", "dnsmasq-dhcp"})
        self.assertEqual(next(x for x in h1.services if x.key == "dnsmasq-dhcp").type, "process")
        names = {(q.name, q.label_by) for q in generic.prometheus.queries}
        self.assertIn(("disk_percent", "mountpoint"), names)
        self.assertIn(("net_rx_bytes_per_sec", "device"), names)
        self.assertIn(("cpu_temp_celsius", None), names)


class SelectorTests(unittest.TestCase):
    def test_expand(self):
        self.assertEqual(expand_selector('x{$SEL}', ''), 'x{}')
        self.assertEqual(expand_selector('x{mode="idle",$SEL}', ''), 'x{mode="idle"}')
        self.assertEqual(expand_selector('x{mode="idle",$SEL}', 'instance="h1"'), 'x{mode="idle",instance="h1"}')
        self.assertEqual(expand_selector('x{$SEL}', 'instance="h1"'), 'x{instance="h1"}')


class CheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def slow(h, b):
            time.sleep(0.4)
            return 200, {}, b"ok"
        cls.srv, cls.cls, cls.url = start_server({
            "/ok": lambda h, b: (200, {}, b"Healthy"),
            "/err": lambda h, b: (500, {}, b"boom"),
            "/redir": lambda h, b: (302, {"Location": "/ok"}, b""),
            "/slow": slow,
        })

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def http(self, path, **kw):
        sc = C.ServiceCheck(key="t", name="t", type="http", url=self.url + path, retries=0, timeout_seconds=2, **kw)
        return run_check(sc)

    def test_ok(self):
        r = self.http("/ok")
        self.assertEqual((r.status, r.http_status), ("operational", 200))
        self.assertIsNotNone(r.response_ms)

    def test_status_down(self):
        r = self.http("/err")
        self.assertEqual((r.status, r.http_status), ("down", 500))
        self.assertIn("500", r.error)

    def test_expected_status_custom(self):
        self.assertEqual(self.http("/err", expected_status=[(500, 500)]).status, "operational")

    def test_redirect_followed(self):
        self.assertEqual(self.http("/redir").status, "operational")

    def test_body_match(self):
        self.assertEqual(self.http("/ok", expect_body_contains="Healthy").status, "operational")
        self.assertEqual(self.http("/ok", expect_body_contains="Nope").status, "down")

    def test_degraded_when_slow(self):
        self.assertEqual(self.http("/slow", degraded_after_ms=100).status, "degraded")

    def test_timeout_is_down(self):
        sc = C.ServiceCheck(key="t", name="t", type="http", url=self.url + "/slow", retries=0, timeout_seconds=0.1)
        r = run_check(sc)
        self.assertEqual(r.status, "down")

    def test_connection_refused_and_retry(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        sleeps = []
        sc = C.ServiceCheck(key="t", name="t", type="http", url=f"http://127.0.0.1:{port}/", retries=2, timeout_seconds=1)
        r = run_check(sc, sleep=sleeps.append)
        self.assertEqual(r.status, "down")
        self.assertEqual(len(sleeps), 2)

    def test_tcp(self):
        srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1); port = srv.getsockname()[1]
        sc = C.ServiceCheck(key="s", name="s", type="tcp", host="127.0.0.1", port=port, retries=0)
        self.assertEqual(run_check(sc).status, "operational")
        srv.close()
        self.assertEqual(run_check(sc).status, "down")


class PrometheusTests(unittest.TestCase):
    def srv(self, query_fn, targets=None):
        routes = {"/api/v1/query": query_fn,
                  "/api/v1/targets": lambda h, b: jresp({"status": "success", "data": {"activeTargets": targets if targets is not None else [
                      {"health": "up", "labels": {"job": "node", "instance": "h1:9100"}},
                      {"health": "down", "labels": {"job": "cadvisor", "instance": "h1:8080"}}]}})}
        s, cls, url = start_server(routes)
        self.addCleanup(s.shutdown)
        return url, cls

    def cfg(self, url, names=("cpu_percent", "mem_percent")):
        return C.PrometheusConfig(enabled=True, base_url=url, timeout_seconds=2,
                                  queries=[C.PromQuery(n, f"m_{n}{{$SEL}}") for n in names])

    def test_ok_and_targets(self):
        url, _ = self.srv(lambda h, b: prom_vector(12.5))
        r = collect(self.cfg(url))
        self.assertEqual((r.prom_status, r.metrics_status), ("ok", "ok"))
        self.assertEqual(r.values, {("cpu_percent", ""): 12.5, ("mem_percent", ""): 12.5})
        self.assertEqual((r.targets_up, r.targets_total), (1, 2))
        self.assertEqual(r.down_targets, ["cadvisor/h1:8080"])

    def test_empty_is_unavailable_not_invented(self):
        def q(h, b):
            return prom_empty() if "mem_percent" in h.path else prom_vector(5)
        url, _ = self.srv(q)
        r = collect(self.cfg(url))
        self.assertEqual(r.metrics_status, "partial")
        self.assertEqual(r.unavailable, ["mem_percent"])
        self.assertNotIn(("mem_percent", ""), r.values)

    def test_all_empty_is_failed(self):
        url, _ = self.srv(lambda h, b: prom_empty())
        r = collect(self.cfg(url))
        self.assertEqual((r.prom_status, r.metrics_status), ("ok", "failed"))

    def test_query_error(self):
        url, _ = self.srv(lambda h, b: jresp({"status": "error", "errorType": "bad_data", "error": "parse error"}, 400))
        r = collect(self.cfg(url))
        self.assertEqual(r.metrics_status, "failed")
        self.assertIn("cpu_percent", r.errors)

    def test_unreachable_never_raises(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        r = collect(self.cfg(f"http://127.0.0.1:{port}"))
        self.assertEqual((r.prom_status, r.metrics_status), ("unreachable", "failed"))
        self.assertTrue(r.prom_error)

    def test_disabled(self):
        r = collect(C.PrometheusConfig(enabled=False))
        self.assertEqual((r.prom_status, r.metrics_status), ("disabled", "disabled"))

    def test_nan_dropped_and_reduce(self):
        def q(h, b):
            return jresp({"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {}, "value": [1, "NaN"]}, {"metric": {}, "value": [1, "2"]}, {"metric": {}, "value": [1, "4"]}]}})
        url, _ = self.srv(q)
        cfg = self.cfg(url, ("cpu_percent",))
        cfg.queries[0].reduce = "avg"
        self.assertEqual(collect(cfg).values[("cpu_percent", "")], 3.0)

    def test_selector_sent(self):
        url, cls = self.srv(lambda h, b: prom_vector(1))
        cfg = self.cfg(url, ("cpu_percent",))
        cfg.selector = 'instance="h1:9100"'
        collect(cfg)
        qs = [r["path"] for r in cls.log_requests if "/api/v1/query" in r["path"]]
        self.assertIn("instance%3D%22h1%3A9100%22", qs[0])


class LocalMetricsTests(unittest.TestCase):
    def fake_proc(self, cpu, rx=1000, tx=500, mounts=None):
        d = tempfile.mkdtemp()
        os.makedirs(f"{d}/net")
        open(f"{d}/stat", "w").write(f"cpu  {cpu[0]} 0 {cpu[1]} {cpu[2]} 0 0 0 0 0 0\ncpu0 1 1 1 1 0 0 0 0 0 0\n")
        open(f"{d}/meminfo", "w").write("MemTotal:       4000000 kB\nMemAvailable:   1000000 kB\n")
        open(f"{d}/loadavg", "w").write("0.50 0.40 0.30 1/100 123\n")
        open(f"{d}/mounts", "w").write(mounts or "")
        open(f"{d}/net/dev", "w").write("Inter-|   Receive\n face |bytes\n"
                                          f"  lo: 999999 1 0 0 0 0 0 0 999999 1 0 0 0 0 0 0\n eth0: {rx} 1 0 0 0 0 0 0 {tx} 1 0 0 0 0 0 0\n"
                                          f" eth1: {rx * 2} 1 0 0 0 0 0 0 {tx * 2} 1 0 0 0 0 0 0\n")
        return d

    def test_values_per_nic_and_total(self):
        t = [100.0]
        lc = LocalCollector(["/"], ["lo"], clock=lambda: t[0], sys_root="/nonexistent")
        lc.proc = self.fake_proc((100, 0, 900))
        first = lc.collect()
        self.assertNotIn(("cpu_percent", ""), first)                  # needs two samples; not invented
        self.assertEqual(first[("mem_percent", "")], 75.0)
        self.assertEqual(first[("load1", "")], 0.5)
        lc.proc = self.fake_proc((200, 0, 1000), rx=3000, tx=1500)
        t[0] = 102.0
        second = lc.collect()
        self.assertEqual(second[("cpu_percent", "")], 50.0)
        self.assertEqual(second[("net_rx_bytes_per_sec", "eth0")], 1000.0)   # (3000-1000)/2
        self.assertEqual(second[("net_rx_bytes_per_sec", "eth1")], 2000.0)   # second physical NIC
        self.assertEqual(second[("net_rx_bytes_per_sec", "")], 3000.0)       # combined total of both NICs
        self.assertEqual(second[("net_tx_bytes_per_sec", "")], 1500.0)
        self.assertFalse(any(k[1] == "lo" for k in second))
        self.assertIn(("disk_percent", "/"), second)

    def test_multiple_drives_detected_and_filtered(self):
        mounts = ("/dev/sda2 / ext4 rw 0 0\n/dev/sda1 /boot/efi vfat rw 0 0\n/dev/sdb1 /mnt/media ext4 rw 0 0\n"
                  "/dev/sdc1 /mnt/with\\040space xfs rw 0 0\ntmpfs /run tmpfs rw 0 0\noverlay /var/lib/docker/overlay2/x overlay rw 0 0\n"
                  "/dev/sdb1 /srv/bindmount ext4 rw 0 0\n/dev/loop0 /snap/core/1 squashfs ro 0 0\n/dev/sdd1 /var/lib/docker/volumes ext4 rw 0 0\n")
        from hcluster_agent.localmetrics import list_drives
        proc = self.fake_proc((1, 2, 3), mounts=mounts)
        self.assertEqual(list_drives(proc), ["/", "/mnt/media", "/mnt/with space"])   # efi, tmpfs, overlay, bind dup, squashfs, docker dirs skipped
        self.assertEqual(list_drives(proc, only=["/data"]), ["/data"])

    def test_cpu_temp_hwmon_and_fallbacks(self):
        from hcluster_agent.localmetrics import read_cpu_temp
        sysd = tempfile.mkdtemp()
        hw = f"{sysd}/class/hwmon/hwmon3"; os.makedirs(hw)
        open(f"{hw}/name", "w").write("coretemp\n")
        open(f"{hw}/temp1_input", "w").write("55000\n"); open(f"{hw}/temp1_label", "w").write("Package id 0\n")
        open(f"{hw}/temp2_input", "w").write("61000\n"); open(f"{hw}/temp2_label", "w").write("Core 0\n")
        self.assertEqual(read_cpu_temp(sysd), 55.0)                       # package sensor preferred
        os.remove(f"{hw}/temp1_label")
        self.assertEqual(read_cpu_temp(sysd), 61.0)                       # else hottest sensor
        other = tempfile.mkdtemp()
        z = f"{other}/class/thermal/thermal_zone0"; os.makedirs(z)
        open(f"{z}/type", "w").write("x86_pkg_temp\n"); open(f"{z}/temp", "w").write("48500\n")
        self.assertEqual(read_cpu_temp(other), 48.5)                      # thermal_zone fallback
        self.assertIsNone(read_cpu_temp(tempfile.mkdtemp()))              # no sensor => None, not invented
        open(f"{hw}/temp2_input", "w").write("0\n"); open(f"{hw}/temp1_input", "w").write("999000\n")
        self.assertIsNone(read_cpu_temp(sysd))                            # absurd readings rejected

    def test_temp_reaches_collector(self):
        sysd = tempfile.mkdtemp(); hw = f"{sysd}/class/hwmon/hwmon0"; os.makedirs(hw)
        open(f"{hw}/name", "w").write("k10temp\n"); open(f"{hw}/temp1_input", "w").write("42000\n")
        lc = LocalCollector(["/"], [], sys_root=sysd)
        lc.proc = self.fake_proc((1, 2, 3))
        self.assertEqual(lc.collect()[("cpu_temp_celsius", "")], 42.0)

    def test_missing_proc_does_not_raise(self):
        lc = LocalCollector(["/nonexistent-mount"], [], proc="/nonexistent-proc", sys_root="/nonexistent")
        self.assertEqual(lc.collect(), {})


class ProcessCheckTests(unittest.TestCase):
    def fake(self, comms=("systemd", "dnsmasq"), udp=(67,), tcp=()):
        d = tempfile.mkdtemp(); os.makedirs(f"{d}/net")
        for i, c in enumerate(comms):
            os.makedirs(f"{d}/{100 + i}"); open(f"{d}/{100 + i}/comm", "w").write(c + "\n")
        os.makedirs(f"{d}/self")                                           # non-numeric dirs are ignored
        hdr = "  sl  local_address rem_address   st\n"
        open(f"{d}/net/udp", "w").write(hdr + "".join(f"  0: 0132A8C0:{p:04X} 00000000:0000 07 0\n" for p in udp))
        open(f"{d}/net/tcp", "w").write(hdr + "".join(f"  0: 00000000:{p:04X} 00000000:0000 0A 0\n" for p in tcp) + "  1: 00000000:0050 0A0A0A0A:0001 01 0\n")
        return d

    def chk(self, proc, **kw):
        from hcluster_agent.checks import _process_once
        return _process_once(C.ServiceCheck(key="p", name="p", type="process", process_name="dnsmasq", **kw), proc)

    def test_running_and_listening(self):
        self.assertEqual(self.chk(self.fake(), listen_udp=[67]).status, "operational")
        self.assertEqual(self.chk(self.fake(tcp=(8080,)), listen_tcp=[8080]).status, "operational")

    def test_not_running_or_not_bound(self):
        r = self.chk(self.fake(comms=("systemd",)), listen_udp=[67]); self.assertEqual(r.status, "down"); self.assertIn("not running", r.error)
        r = self.chk(self.fake(udp=()), listen_udp=[67]); self.assertEqual(r.status, "down"); self.assertIn("udp/67", r.error)
        self.assertEqual(self.chk(self.fake(tcp=()), listen_tcp=[80]).status, "down")          # an ESTABLISHED (0x01) socket is not a listener

    def test_long_names_truncated_like_the_kernel(self):
        from hcluster_agent.checks import _process_once
        sc = C.ServiceCheck(key="p", name="p", type="process", process_name="averyveryverylongprocessname")
        self.assertEqual(_process_once(sc, self.fake(comms=("averyveryverylo",))).status, "operational")

    def test_real_proc(self):
        import socket as so
        s = so.socket(so.AF_INET, so.SOCK_DGRAM); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
        me = open("/proc/self/comm").read().strip()
        from hcluster_agent.checks import _process_once
        sc = C.ServiceCheck(key="p", name="p", type="process", process_name=me, listen_udp=[port])
        self.assertEqual(_process_once(sc).status, "operational")
        s.close()
        self.assertEqual(_process_once(sc).status, "down")

    def test_config(self):
        cfg = C.load_config(write_cfg(f"node: {{id: '{NODE}'}}\napi: {{url: 'https://x.example.com'}}\nservices:\n  - {{key: dm, type: process, process_name: dnsmasq, listen_udp: [67]}}\n"), {"HCLUSTER_TOKEN": "t"})
        self.assertEqual(cfg.services[0].target(), "process dnsmasq listening udp/67")
        with self.assertRaises(C.ConfigError):
            C.load_config(write_cfg(f"node: {{id: '{NODE}'}}\napi: {{url: 'https://x.example.com'}}\nservices:\n  - {{key: dm, type: process}}\n"), {"HCLUSTER_TOKEN": "t"})


class LabeledPrometheusTests(unittest.TestCase):
    def test_label_by_keeps_every_series_and_sums_total(self):
        def q(h, b):
            return jresp({"status": "success", "data": {"resultType": "vector", "result": [
                {"metric": {"mountpoint": "/mnt/media"}, "value": [1, "85"]}, {"metric": {"mountpoint": "/"}, "value": [1, "40"]},
                {"metric": {}, "value": [1, "7"]}]}})            # series without the label are ignored
        srv, cls, url = start_server({"/api/v1/targets": lambda h, b: jresp({"status": "success", "data": {"activeTargets": []}}), "/api/v1/query": q})
        self.addCleanup(srv.shutdown)
        cfg = C.PrometheusConfig(enabled=True, base_url=url, timeout_seconds=2,
                                 queries=[C.PromQuery("disk_percent", "x", label_by="mountpoint"), C.PromQuery("net_rx_bytes_per_sec", "y", label_by="mountpoint", sum_total=True)])
        r = collect(cfg)
        self.assertEqual(r.values[("disk_percent", "/")], 40.0)
        self.assertEqual(r.values[("disk_percent", "/mnt/media")], 85.0)
        self.assertNotIn(("disk_percent", ""), r.values)
        self.assertEqual(r.values[("net_rx_bytes_per_sec", "")], 125.0)     # sum_total adds the node-wide value
        self.assertEqual(r.metrics_status, "ok")


class DnsCheckTests(unittest.TestCase):
    def serve(self, rcode=0, wrong_id=False, reply=True):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); sock.bind(("127.0.0.1", 0))
        self.addCleanup(sock.close)
        def loop():
            sock.settimeout(3)
            try:
                data, addr = sock.recvfrom(512)
            except OSError:
                return
            if not reply:
                return
            qid = data[:2] if not wrong_id else b"\x00\x00"
            sock.sendto(qid + bytes([0x81, 0x80 | rcode]) + data[4:], addr)
        import threading; threading.Thread(target=loop, daemon=True).start()
        return sock.getsockname()[1]

    def chk(self, port, **kw):
        return run_check(C.ServiceCheck(key="d", name="d", type="dns", host="127.0.0.1", port=port, retries=0, timeout_seconds=1, **kw))

    def test_ok_nxdomain_and_errors(self):
        self.assertEqual(self.chk(self.serve(0)).status, "operational")
        self.assertEqual(self.chk(self.serve(3)).status, "operational")      # NXDOMAIN still proves the resolver works
        r = self.chk(self.serve(2)); self.assertEqual(r.status, "down"); self.assertIn("SERVFAIL", r.error)
        self.assertEqual(self.chk(self.serve(5)).status, "down")             # REFUSED
        self.assertEqual(self.chk(self.serve(0, wrong_id=True)).status, "down")
        self.assertEqual(self.chk(self.serve(reply=False)).status, "down")   # timeout

    def test_query_packet(self):
        from hcluster_agent.checks import build_dns_query
        pkt = build_dns_query("example.com", 0x1234)
        self.assertEqual(pkt[:2], b"\x12\x34")
        self.assertIn(b"\x07example\x03com\x00", pkt)

    def test_config_accepts_dns_defaults(self):
        cfg = C.load_config(write_cfg(f"node: {{id: '{NODE}'}}\napi: {{url: 'https://x.example.com'}}\nservices:\n  - {{key: dns1, type: dns}}\n"), {"HCLUSTER_TOKEN": "t"})
        s = cfg.services[0]
        self.assertEqual((s.host, s.port, s.query_name), ("127.0.0.1", 53, "example.com"))


class ReporterTests(unittest.TestCase):
    def setup(self, handler, **over):
        s, cls, url = start_server({"/api/agent/report": handler})
        self.addCleanup(s.shutdown)
        t = [0.0]
        rep = Reporter(mkcfg(api_url=url, **over), clock=lambda: t[0])
        return rep, cls, t

    def test_success_sends_auth_and_batches(self):
        rep, cls, t = self.setup(lambda h, b: jresp({"ok": True}))
        for i in range(25):
            rep.enqueue({"collected_at": f"2026-01-01T00:00:{i:02d}Z"})
        self.assertEqual(rep.flush(), 25)
        self.assertEqual(len(rep.queue), 0)
        self.assertEqual(len(cls.log_requests), 2)                         # 20 + 5
        r0 = cls.log_requests[0]
        self.assertEqual(r0["headers"]["Authorization"], "Bearer hcs_test")
        import json
        self.assertEqual(json.loads(r0["body"])["node_id"], NODE)

    def test_outage_queues_backs_off_then_recovers(self):
        state = {"up": False}
        rep, cls, t = self.setup(lambda h, b: jresp({"ok": True}) if state["up"] else (503, {}, b"down"))
        rep.enqueue({"collected_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(rep.flush(), 0)
        self.assertEqual(len(rep.queue), 1)
        self.assertEqual(rep.failures, 1)
        calls = len(cls.log_requests)
        rep.enqueue({"collected_at": "2026-01-01T00:01:00Z"})
        rep.flush()                                                         # inside backoff window => no request
        self.assertEqual(len(cls.log_requests), calls)
        state["up"] = True
        t[0] += 1000
        self.assertEqual(rep.flush(), 2)                                    # backlog delivered in order, one batch
        self.assertEqual((len(rep.queue), rep.failures), (0, 0))

    def test_network_down(self):
        s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
        rep = Reporter(mkcfg(api_url=f"http://127.0.0.1:{port}"), clock=lambda: 0.0)
        rep.enqueue({"collected_at": "x"})
        self.assertEqual(rep.flush(), 0)
        self.assertEqual(len(rep.queue), 1)

    def test_auth_failure_keeps_queue_and_waits_long(self):
        rep, cls, t = self.setup(lambda h, b: (401, {}, b'{"error":"unauthorized"}'))
        rep.enqueue({"collected_at": "x"})
        rep.flush()
        self.assertEqual(len(rep.queue), 1)
        self.assertGreaterEqual(rep.next_attempt - t[0], 60)

    def test_permanent_rejection_drops_poison_batch(self):
        rep, cls, t = self.setup(lambda h, b: (400, {}, b'{"error":"invalid"}'))
        rep.enqueue({"collected_at": "x"})
        rep.flush()
        self.assertEqual(len(rep.queue), 0)

    def test_rate_limited_honours_retry_after(self):
        rep, cls, t = self.setup(lambda h, b: (429, {"Retry-After": "42"}, b""))
        rep.enqueue({"collected_at": "x"})
        rep.flush()
        self.assertEqual(rep.next_attempt, 42.0)

    def test_redirect_is_not_followed(self):
        other, ocls, ourl = start_server({"/steal": lambda h, b: jresp({})})
        self.addCleanup(other.shutdown)
        rep, cls, t = self.setup(lambda h, b: (307, {"Location": ourl + "/steal"}, b""))
        rep.enqueue({"collected_at": "x"})
        rep.flush()
        self.assertEqual(ocls.log_requests, [])                              # token never leaked to the redirect target
        self.assertEqual(len(rep.queue), 1)

    def test_queue_bounded_and_persisted(self):
        cfg = mkcfg(max_queue=3)
        rep = Reporter(cfg, clock=lambda: 0.0)
        for i in range(5):
            rep.enqueue({"n": i})
        self.assertEqual([r["n"] for r in rep.queue], [2, 3, 4])
        self.assertEqual(rep.dropped, 2)
        again = Reporter(cfg, clock=lambda: 0.0)
        self.assertEqual([r["n"] for r in again.queue], [2, 3, 4])


class AgentTests(unittest.TestCase):
    def build(self, prom_url=None, services=None, fallback=True):
        pc = C.PrometheusConfig(enabled=bool(prom_url), base_url=prom_url or "http://127.0.0.1:1", timeout_seconds=1,
                                queries=[C.PromQuery("cpu_percent", "cpu"), C.PromQuery("net_rx_bytes_per_sec", "rx")])
        cfg = mkcfg(prometheus=pc, services=services or [], local_fallback=fallback)
        t = [0.0]
        rep = Reporter(cfg, clock=lambda: t[0])
        return Agent(cfg, rep, now=lambda: t[0]), t

    def test_full_report(self):
        s, cls, url = start_server({"/ok": lambda h, b: (200, {}, b"x"), "/err": lambda h, b: (500, {}, b"x")})
        self.addCleanup(s.shutdown)
        svcs = [C.ServiceCheck(key="up", name="Up", type="http", url=url + "/ok", retries=0),
                C.ServiceCheck(key="dn", name="Down", type="http", url=url + "/err", retries=0)]
        ps, pc, purl = start_server({"/api/v1/targets": lambda h, b: jresp({"status": "success", "data": {"activeTargets": []}}),
                                     "/api/v1/query": lambda h, b: prom_vector(7) if "cpu" in h.path else prom_empty()})
        self.addCleanup(ps.shutdown)
        agent, t = self.build(purl, svcs)
        rep = agent.step(force=True)
        self.assertEqual({x["key"]: x["status"] for x in rep["services"]}, {"up": "operational", "dn": "down"})
        mr = rep["metrics_report"]
        self.assertEqual(mr["prometheus"]["status"], "ok")
        vals = {v["name"]: v for v in mr["values"]}
        self.assertEqual(vals["cpu_percent"]["source"], "prometheus")
        self.assertNotIn("label", vals["cpu_percent"])                     # node-wide metrics carry no label
        self.assertIn("host", rep)
        self.assertNotIn("token", str(rep).lower())

    def test_prometheus_down_node_keeps_reporting(self):
        svc = C.ServiceCheck(key="t", name="T", type="tcp", host="127.0.0.1", port=1, retries=0, timeout_seconds=0.5)
        agent, t = self.build("http://127.0.0.1:1", [svc])
        rep = agent.step(force=True)
        self.assertEqual(rep["metrics_report"]["prometheus"]["status"], "unreachable")
        self.assertEqual(rep["metrics_report"]["status"], "failed")
        self.assertEqual(len(rep["services"]), 1)                           # service checks still ran
        self.assertEqual(len(agent.reporter.queue), 1)

    def test_missing_metric_reported_unavailable_when_no_fallback(self):
        ps, pc, purl = start_server({"/api/v1/targets": lambda h, b: jresp({"status": "success", "data": {"activeTargets": []}}),
                                     "/api/v1/query": lambda h, b: prom_vector(7) if "cpu" in h.path else prom_empty()})
        self.addCleanup(ps.shutdown)
        agent, t = self.build(purl, fallback=False)
        mr = agent.step(force=True)["metrics_report"]
        self.assertEqual(mr["status"], "partial")
        self.assertEqual(mr["unavailable"], ["net_rx_bytes_per_sec"])
        self.assertNotIn("net_rx_bytes_per_sec", [v["name"] for v in mr["values"]])

    def test_prometheus_disabled_uses_local(self):
        agent, t = self.build(None)
        agent.local.collect()
        mr = agent.step(force=True)["metrics_report"]
        self.assertEqual(mr["prometheus"]["status"], "disabled")
        self.assertTrue(all(v["source"] == "local" for v in mr["values"]))

    def test_independent_intervals(self):
        svc = C.ServiceCheck(key="t", name="T", type="tcp", host="127.0.0.1", port=1, retries=0, timeout_seconds=0.2, interval_seconds=30)
        agent, t = self.build(None, [svc])
        agent.cfg.heartbeat_seconds, agent.cfg.metrics_seconds = 60, 120
        r = agent.step()                      # t=0: everything due
        self.assertIn("services", r); self.assertIn("metrics_report", r)
        t[0] = 10
        self.assertIsNone(agent.step())       # nothing due
        t[0] = 31
        r = agent.step()                      # only the service interval elapsed
        self.assertIn("services", r); self.assertNotIn("metrics_report", r)
        t[0] = 95
        r = agent.step()
        self.assertIn("services", r); self.assertNotIn("metrics_report", r)
        t[0] = 200
        self.assertIn("metrics_report", agent.step())

    def test_disabled_service_not_checked(self):
        svc = C.ServiceCheck(key="t", name="T", type="tcp", host="127.0.0.1", port=1, enabled=False)
        agent, t = self.build(None, [svc])
        self.assertNotIn("services", agent.step(force=True))


if __name__ == "__main__":
    unittest.main(verbosity=1)
