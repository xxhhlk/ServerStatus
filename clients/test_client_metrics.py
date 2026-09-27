import io
import runpy
import sys
import threading
import time
import types
import unittest
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock


CLIENT_DIR = Path(__file__).resolve().parent


def load_client(filename):
    if filename == "client-psutil.py" and "psutil" not in sys.modules:
        try:
            __import__("psutil")
        except ImportError:
            sys.modules["psutil"] = types.ModuleType("psutil")
    return runpy.run_path(str(CLIENT_DIR / filename))


def open_from(mapping):
    """按路径返回内容；不在映射里的路径抛 FileNotFoundError（模拟该文件不存在）。
    用 StringIO 而非 mock_open：后者不支持 next()，表头不会被跳过。"""
    def _open(path, *args, **kwargs):
        if path in mapping:
            return io.StringIO(mapping[path])
        raise FileNotFoundError(path)
    return _open


class _LoopStop(Exception):
    """用来从 while True 的线程体里退出（替换 time.sleep 时抛出）。"""


def run_loop_with_fake_sleep(fn, sleep_hook):
    """跑客户端里 while True 的线程体：把 fn 所在命名空间的 time.sleep 换成 sleep_hook
    （由它抛 _LoopStop 结束循环）。只改 runpy 命名空间里的 time，不动真实 time 模块。"""
    with mock.patch.dict(fn.__globals__, {"time": types.SimpleNamespace(sleep=sleep_hook)}):
        try:
            fn()
        except _LoopStop:
            pass


class ClientMetricTests(unittest.TestCase):
    # --- 网卡过滤 ---------------------------------------------------------
    # 本分支用「物理性」判定而非名字黑名单：Linux 看 /sys/class/net/<if>/device，
    # Windows 看 WMI PNPDeviceID 前缀。以下测试锁定该策略，防止退回黑名单。

    def test_psutil_counters_have_exactly_one_call_site(self):
        """唯一调用点是不翻倍的根本原因：任何新增调用点都会让并发风险回归。"""
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        self.assertEqual(source.count("psutil.net_io_counters("), 1)

    def test_net_monitor_is_the_only_psutil_reader_under_concurrency(self):
        client = load_client("client-psutil.py")
        counters = namedtuple("Counters", "bytes_sent bytes_recv")
        active = 0
        maximum_active = 0
        state_lock = threading.Lock()

        def fake_counters(*_args, **_kwargs):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
            time.sleep(0.002)
            with state_lock:
                active -= 1
            return {"eth0": counters(500, 1000)}

        client_globals = client["_net_monitor"].__globals__
        with mock.patch.object(client["psutil"], "net_io_counters", side_effect=fake_counters, create=True), \
                mock.patch.dict(client_globals, {"is_virtual_nic": lambda _name: False, "INTERVAL": 0.001}):
            monitor = threading.Thread(target=client["_net_monitor"], daemon=True)
            monitor.start()
            deadline = time.time() + 5
            while client["liuliang"]() == (0, 0) and time.time() < deadline:
                time.sleep(0.01)
            with ThreadPoolExecutor(max_workers=12) as executor:
                results = list(executor.map(lambda _index: client["liuliang"](), range(48)))

        self.assertEqual(maximum_active, 1)
        self.assertEqual(results, [(1000, 500)] * 48)

    def test_psutil_physical_counters_exclude_virtual_interfaces(self):
        client = load_client("client-psutil.py")
        counters = namedtuple("Counters", "bytes_sent bytes_recv")
        values = {
            "eth0": counters(500, 1000),
            "ens5": counters(300, 700),
            "wlo1": counters(200, 400),
            "lo": counters(9000, 9000),
            "docker0": counters(8000, 8000),
            "veth123": counters(7000, 7000),
        }
        virtual = {"lo", "docker0", "veth123"}
        with mock.patch.dict(client["_sum_physical_counters"].__globals__,
                             {"is_virtual_nic": lambda name: name in virtual}):
            self.assertEqual(client["_sum_physical_counters"](values), (2100, 1000))

    def test_psutil_linux_interface_filter_uses_sysfs_device_link(self):
        client = load_client("client-psutil.py")
        device_links = {"/sys/class/net/eth0/device", "/sys/class/net/wlan0/device"}
        with mock.patch.object(client["sys"], "platform", "linux"), \
                mock.patch.object(client["os"], "access", return_value=True), \
                mock.patch.object(client["os"].path, "exists",
                                  side_effect=lambda path: path in device_links):
            predicate = client["is_virtual_nic"]
            self.assertTrue(predicate("lo"))
            self.assertFalse(predicate("eth0"))
            self.assertFalse(predicate("wlan0"))
            # 名字黑名单曾漏掉 ifb*/tailscale*，sysfs 判定可自动覆盖
            for name in ("ifb0", "tailscale0", "docker0", "veth123", "br-abc", "bond0"):
                self.assertTrue(predicate(name), name)

    def test_linux_totals_read_proc_and_exclude_virtual_interfaces(self):
        client = load_client("client-linux.py")
        proc_net_dev = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth0: 1000 10 0 0 0 0 0 0 500 5 0 0 0 0 0 0
  ens5: 700 7 0 0 0 0 0 0 300 3 0 0 0 0 0 0
  wlo1: 400 4 0 0 0 0 0 0 200 2 0 0 0 0 0 0
    lo: 9000 9 0 0 0 0 0 0 9000 9 0 0 0 0 0 0
veth123: 8000 8 0 0 0 0 0 0 8000 8 0 0 0 0 0 0
"""
        physical = {"eth0", "ens5", "wlo1"}
        with mock.patch("builtins.open", mock.mock_open(read_data=proc_net_dev)), \
                mock.patch.dict(client["liuliang"].__globals__,
                                {"_is_physical_interface": lambda name: name in physical}):
            self.assertEqual(client["liuliang"](), (2100, 1000))

    def test_linux_interface_filter_uses_sysfs_device_link(self):
        client = load_client("client-linux.py")
        # _is_physical_interface 用 os.path.join 拼路径，Windows 上分隔符不同，
        # 故按同样方式构造期望路径，保证测试跨平台可跑
        device_links = {
            client["os"].path.join("/sys/class/net", name, "device")
            for name in ("eth0", "wlan0")
        }
        with mock.patch.object(client["os"].path, "exists",
                               side_effect=lambda path: path in device_links):
            predicate = client["_is_physical_interface"]
            self.assertTrue(predicate("eth0"))
            self.assertTrue(predicate("wlan0"))
            for name in ("lo", "ifb0", "tailscale0", "docker0", "veth123", "br-abc", "bond0"):
                self.assertFalse(predicate(name), name)

    def test_linux_interface_filter_result_is_cached(self):
        """物理性分类是静态的，必须按接口名缓存：liuliang 与 _net_speed 每轮各调一遍，
        不缓存等于每秒对每个接口重复 stat。"""
        client = load_client("client-linux.py")
        exists_calls = []
        device_links = {client["os"].path.join("/sys/class/net", name, "device")
                        for name in ("eth0", "wlan0")}

        def fake_exists(path):
            exists_calls.append(path)
            return path in device_links

        with mock.patch.object(client["os"].path, "exists", side_effect=fake_exists):
            predicate = client["_is_physical_interface"]
            for _ in range(3):
                self.assertTrue(predicate("eth0"))
                self.assertTrue(predicate("wlan0"))
                self.assertFalse(predicate("lo"))
        self.assertEqual(len(exists_calls), 3)   # 三个接口各只 stat 一次

    def test_interface_filter_keeps_wlo_and_excludes_virtual_interfaces(self):
        """回归：名字子串匹配 'lo' in k 会误杀 wlo1 无线网卡（上游 050b107 修复点）。
        本分支改用 sysfs 物理性判定，天然不受子串误杀影响。"""
        client_linux = load_client("client-linux.py")
        client_psutil = load_client("client-psutil.py")
        # wlo1/wlan0 无线网卡必须计入；lo 及各类虚拟接口必须排除
        device_links = {
            client_linux["os"].path.join("/sys/class/net", name, "device")
            for name in ("eth0", "wlo1", "enp3s0")
        }
        with mock.patch.object(client_linux["os"].path, "exists",
                               side_effect=lambda path: path in device_links):
            for name in ("wlo1", "eth0", "enp3s0"):
                self.assertTrue(client_linux["_is_physical_interface"](name), name)
            for name in ("lo", "lo0", "tun0", "docker0", "veth123", "br-test", "vmbr0",
                         "vnet0", "kube-ipvs0", "ifb0", "tailscale0"):
                self.assertFalse(client_linux["_is_physical_interface"](name), name)

        psutil_links = {"/sys/class/net/eth0/device", "/sys/class/net/wlo1/device"}
        with mock.patch.object(client_psutil["sys"], "platform", "linux"), \
                mock.patch.object(client_psutil["os"], "access", return_value=True), \
                mock.patch.object(client_psutil["os"].path, "exists",
                                  side_effect=lambda path: path in psutil_links):
            for name in ("wlo1", "eth0"):
                self.assertFalse(client_psutil["is_virtual_nic"](name), name)
            for name in ("lo", "lo0", "tun0", "docker0", "veth123", "ifb0", "tailscale0"):
                self.assertTrue(client_psutil["is_virtual_nic"](name), name)

    def test_linux_totals_keep_one_way_interfaces(self):
        """回归：旧实现丢弃 rx==0 或 tx==0 的接口，会漏掉单向链路（上游 050b107 修复点）。
        本分支保留该行为：只按物理性过滤，不再按单向零值过滤。"""
        client = load_client("client-linux.py")
        proc_net_dev = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
  eth0: 1000 10 0 0 0 0 0 0 0 0 0 0 0 0 0 0
  ens5: 0 0 0 0 0 0 0 0 300 3 0 0 0 0 0 0
"""
        physical = {"eth0", "ens5"}
        with mock.patch("builtins.open", mock.mock_open(read_data=proc_net_dev)), \
                mock.patch.dict(client["liuliang"].__globals__,
                                {"_is_physical_interface": lambda name: name in physical}):
            self.assertEqual(client["liuliang"](), (1000, 300))

    # --- 网速 -------------------------------------------------------------

    def test_network_speed_starts_and_resets_at_zero(self):
        for filename in ("client-linux.py", "client-psutil.py"):
            with self.subTest(client=filename):
                client = load_client(filename)
                state = client["update_net_speed"].__globals__["netSpeed"]
                state.update({"clock": 0.0, "diff": 0.0, "avgrx": 0, "avgtx": 0, "netrx": 0, "nettx": 0})

                self.assertEqual(client["update_net_speed"](1000, 1000, 100.0), (0, 0))
                self.assertEqual(client["update_net_speed"](1400, 1300, 102.0), (200, 150))
                # 计数器回绕（total < prev）→ 置 0，不产生负数尖峰
                self.assertEqual(client["update_net_speed"](10, 1500, 103.0), (0, 200))
                self.assertEqual(client["update_net_speed"](5, 4, 104.0), (0, 0))

    # --- 平台识别 ---------------------------------------------------------

    def test_os_detection_uses_linux_distribution_id(self):
        os_release = 'NAME="Alpine Linux"\nID=alpine\nVERSION_ID=3.22\n'
        for filename in ("client-linux.py", "client-psutil.py"):
            with self.subTest(client=filename):
                client = load_client(filename)
                with mock.patch.object(client["platform"], "system", return_value="Linux"), \
                        mock.patch("builtins.open", mock.mock_open(read_data=os_release)):
                    self.assertEqual(client["get_os_name"](), "alpine")

    def test_os_detection_has_platform_fallbacks(self):
        psutil_client = load_client("client-psutil.py")
        with mock.patch.object(psutil_client["platform"], "system", return_value="Windows Server 2022"):
            self.assertEqual(psutil_client["get_os_name"](), "windows")

        linux_client = load_client("client-linux.py")
        with mock.patch.object(linux_client["platform"], "system", return_value="FreeBSD"):
            self.assertEqual(linux_client["get_os_name"](), "freebsd")

    def test_cpu_model_prefers_specific_model_and_has_vendor_fallback(self):
        linux_client = load_client("client-linux.py")
        linux_globals = linux_client["get_cpu_model"].__globals__
        with mock.patch.dict(linux_globals, {
            "get_cpuinfo_values": lambda: {"model name": "AMD EPYC 7B13"},
            "get_lscpu_info": lambda: {"vendor id": "AuthenticAMD", "architecture": "x86_64"},
        }):
            self.assertEqual(linux_client["get_cpu_model"](), "AMD EPYC 7B13")

        psutil_client = load_client("client-psutil.py")
        platform_module = psutil_client["platform"]
        uname = types.SimpleNamespace(processor="", machine="x86_64")
        # 屏蔽本分支新增的 Windows 注册表取值，单独验证厂商回退路径
        with mock.patch.dict(psutil_client["get_cpu_model"].__globals__,
                             {"_win_cpu_model": lambda: None}), \
                mock.patch.object(platform_module, "processor", return_value=""), \
                mock.patch.object(platform_module, "uname", return_value=uname), \
                mock.patch.object(platform_module, "machine", return_value="x86_64"), \
                mock.patch.object(platform_module, "platform", return_value="Linux GenuineIntel"):
            self.assertEqual(psutil_client["get_cpu_model"](), "GenuineIntel")

    def test_psutil_windows_registry_cpu_model_wins(self):
        """本分支增强：Windows 下用注册表 ProcessorNameString 取代 Family/Model 编码。"""
        client = load_client("client-psutil.py")
        with mock.patch.dict(client["get_cpu_model"].__globals__,
                             {"_win_cpu_model": lambda: "12th Gen Intel(R) Core(TM) i9-12900H"}):
            self.assertEqual(client["get_cpu_model"](), "12th Gen Intel(R) Core(TM) i9-12900H")


    # --- DNS 解析节流 / 在线探测间隔 --------------------------------------

    def test_should_resolve_throttles_by_interval(self):
        """_ping_thread 的解析节流：纯函数便于验证，1s 循环 30s 间隔应只解析 3~4 次。"""
        for filename in ("client-linux.py", "client-psutil.py"):
            with self.subTest(client=filename):
                client = load_client(filename)
                should = client["_should_resolve"]
                # last_resolve 为 None → 立即解析（首次、或建连失败后强制重试）
                self.assertTrue(should("cu.tz.cloudcpp.com", None, 0.0, 30))
                # 间隔内不重复解析
                self.assertFalse(should("cu.tz.cloudcpp.com", 0.0, 1.0, 30))
                self.assertFalse(should("cu.tz.cloudcpp.com", 0.0, 29.9, 30))
                # 到达间隔 → 解析
                self.assertTrue(should("cu.tz.cloudcpp.com", 0.0, 30.0, 30))
                # 纯 IPv6 字面量永不解析
                self.assertFalse(should("2409:8057:5c00:30::6", None, 0.0, 30))
                # 模拟 1s 循环推进 100 轮（带状态）：interval=30 → 恰在 0/30/60/90 解析
                last_resolve = None
                resolves = 0
                for step in range(100):
                    now = float(step)
                    if should("cu.tz.cloudcpp.com", last_resolve, now, 30):
                        resolves += 1
                        last_resolve = now
                self.assertEqual(resolves, 4)

    def test_dns_throttle_preserves_connect_probe_frequency(self):
        """回归护栏：DNS_REFRESH_INTERVAL 只节流解析，不得被误接到 INTERVAL 上。
        丢包窗口 = PING_PACKET_HISTORY_LEN × INTERVAL，循环周期必须保持 1s。"""
        for filename in ("client-linux.py", "client-psutil.py"):
            with self.subTest(client=filename):
                client = load_client(filename)
                self.assertEqual(client["INTERVAL"], 1)
                self.assertEqual(client["PING_PACKET_HISTORY_LEN"], 100)
                self.assertEqual(client["DNS_REFRESH_INTERVAL"], 30)
                self.assertEqual(client["NET_PROBE_INTERVAL"], 30)
                source = (CLIENT_DIR / filename).read_text(encoding="utf-8")
                # 解析必须走节流判定，且 IP 在循环外初始化（否则跳过的轮次会退回域名解析）
                self.assertIn("_should_resolve(host, last_resolve, now, DNS_REFRESH_INTERVAL)", source)
                self.assertNotIn("flush dns", source)
                # _net_probe_thread 不再硬编码 10s
                self.assertNotIn("time.sleep(10)\n\nlostRate", source)

    def test_ping_thread_resolves_once_then_reuses_ip(self):
        """端到端：_ping_thread 在多轮内只解析一次，且复用同一 IP 建连。"""
        for filename in ("client-linux.py", "client-psutil.py"):
            with self.subTest(client=filename):
                client = load_client(filename)
                resolve_calls = []
                connect_targets = []
                stop_after = 5
                client_globals = client["_ping_thread"].__globals__
                real_socket = client_globals["socket"]

                def fake_getaddrinfo(host, port, family):
                    resolve_calls.append(host)
                    return [(family, None, None, None, ("10.0.0.9", 0))]

                def fake_create_connection(target, timeout=None):
                    connect_targets.append(target)
                    if len(connect_targets) >= stop_after:
                        raise SystemExit  # 退出无限循环
                    return mock.MagicMock()

                # 只替换这两个函数，保留真实 socket.error（异常类不能被 mock）
                try:
                    with mock.patch.object(real_socket, "getaddrinfo", fake_getaddrinfo), \
                            mock.patch.object(real_socket, "create_connection", fake_create_connection), \
                            mock.patch.dict(client_globals, {"INTERVAL": 0.001,
                                                             "DNS_REFRESH_INTERVAL": 30}):
                        client["_ping_thread"]("cu.tz.cloudcpp.com", "10010", 80)
                except SystemExit:
                    pass

                # 5 轮建连，但 DNS 只解析 1 次（首轮），且每轮都用解析出的 IP
                self.assertEqual(len(connect_targets), stop_after)
                self.assertEqual(resolve_calls, ["cu.tz.cloudcpp.com"])
                self.assertEqual(connect_targets, [("10.0.0.9", 80)] * stop_after)

    # --- 客户端 CPU 占用优化 ----------------------------------------------

    def test_monitor_thread_resolves_once_then_reuses_ip(self):
        """端到端：_monitor_thread 多轮内只解析一次，且复用同一 IP 建连。"""
        client = load_client("client-psutil.py")
        resolve_calls = []
        connect_targets = []
        stop_after = 5
        client_globals = client["_monitor_thread"].__globals__
        real_socket = client_globals["socket"]

        def fake_getaddrinfo(host, port, family):
            resolve_calls.append(host)
            return [(family, None, None, None, ("10.0.0.9", 0))]

        def fake_create_connection(target, timeout=None):
            connect_targets.append(target)
            if len(connect_targets) >= stop_after:
                raise SystemExit  # 退出无限循环
            return mock.MagicMock()

        client_globals["monitorServer"]["m1"] = {"type": "tcp", "host": "example.com:80", "latency": 0}
        try:
            with mock.patch.object(real_socket, "getaddrinfo", fake_getaddrinfo), \
                    mock.patch.object(real_socket, "create_connection", fake_create_connection), \
                    mock.patch.dict(client_globals, {"DNS_REFRESH_INTERVAL": 30}):
                client["_monitor_thread"]("m1", "example.com:80", 0.001, "tcp")
        except SystemExit:
            pass

        self.assertEqual(len(connect_targets), stop_after)
        self.assertEqual(resolve_calls, ["example.com"])
        self.assertEqual(connect_targets, [("10.0.0.9", 80)] * stop_after)

    def test_constant_metrics_are_cached(self):
        """get_uptime/get_os_name 的常量值进程内只取一次，避免每轮重复系统调用/文件读取。"""
        client = load_client("client-psutil.py")
        boot_calls = []
        with mock.patch.object(client["psutil"], "boot_time",
                               side_effect=lambda: boot_calls.append(1) or 1000.0):
            client["get_uptime"]()
            client["get_uptime"]()
        self.assertEqual(len(boot_calls), 1)

        with mock.patch.object(client["platform"], "system", return_value="Linux"), \
                mock.patch("builtins.open", mock.mock_open(read_data='ID=alpine\n')) as open_mock:
            self.assertEqual(client["get_os_name"](), "alpine")
            self.assertEqual(client["get_os_name"](), "alpine")
        self.assertEqual(open_mock.call_count, 1)

    def test_linux_os_name_is_cached(self):
        """get_os_name 的结果是常量，进程内只取一次：platform.system() 与 /etc/os-release
        都不变，主循环每轮重算是纯浪费（client-psutil 已有同样的 _os_name 缓存）。"""
        client = load_client("client-linux.py")
        with mock.patch.object(client["platform"], "system", return_value="Linux") as system_mock, \
                mock.patch("builtins.open", mock.mock_open(read_data='ID=alpine\n')) as open_mock:
            self.assertEqual(client["get_os_name"](), "alpine")
            self.assertEqual(client["get_os_name"](), "alpine")
        self.assertEqual(system_mock.call_count, 1)
        self.assertEqual(open_mock.call_count, 1)

    def test_tupd_sampling_moved_out_of_main_loop(self):
        """tupd 采样移到后台线程：主循环只读 _tupd_snapshot，避免每轮 fork 子进程；
        _disk_io 的进程级遍历结果无人读取，不得回归。"""
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        self.assertIn("target=_tupd_thread", source)
        self.assertIn("array['tcp'], array['udp'], array['process'], array['thread'] = _tupd_snapshot", source)
        self.assertNotIn("psutil.process_iter()", source)
        self.assertEqual(load_client("client-psutil.py")["TUP_INTERVAL"], 3)

    # --- Linux tupd（tcp/udp/进程/线程计数）--------------------------------

    def test_linux_tupd_counts_sockets_from_proc(self):
        """直读 /proc 的口径必须与 ss 一致：tcp 排除 LISTEN(0A)/TIME_WAIT(06)/SYN_RECV(03)，
        udp 只计已连接(01)。状态码是 /proc/net/{tcp,udp}{,6} 每行的第 4 列。"""
        client = load_client("client-linux.py")
        proc_net = {
            "/proc/net/tcp": (
                "  sl  local_address rem_address   st\n"
                "   0: 0100007F:1F90 00000000:0000 0A\n"   # LISTEN     → 排除
                "   1: 0100007F:1F91 0100007F:1F92 01\n"   # ESTABLISHED→ 计入
                "   2: 0100007F:1F93 0100007F:1F94 06\n"   # TIME_WAIT  → 排除
                "   3: 0100007F:1F95 0100007F:1F96 03\n"   # SYN_RECV   → 排除
                "   4: 0100007F:1F97 0100007F:1F98 08\n"   # CLOSE_WAIT → 计入
                "   5: 0100007F:1F99 0100007F:1F9A 02\n"   # SYN_SENT   → 计入
            ),
            # 不提供 tcp6/udp6：IPv6 关闭时就是这种情形，必须走 IOError 分支而不是报错
            "/proc/net/udp": (
                "  sl  local_address rem_address   st\n"
                "   0: 00000000:0044 00000000:0000 07\n"   # 未连接  → 排除
                "   1: 0100007F:0045 0100007F:0046 01\n"   # 已连接  → 计入
            ),
            "/proc/loadavg": "0.00 0.01 0.05 1/857 12345\n",
        }
        with mock.patch("builtins.open", side_effect=open_from(proc_net)), \
                mock.patch.object(client["os"], "listdir",
                                  return_value=["1", "2", "855", "net", "self"]):
            self.assertEqual(client["tupd"](), (3, 1, 3, 857))

    def test_linux_tupd_falls_back_to_ss_when_proc_is_absent(self):
        """无 /proc 的平台（macOS/BSD）必须退回 ss/ps 原实现，不能抛异常——
        原实现缺 ss 时只是管道退化成 -1，直读则会直接抛 OSError。"""
        client = load_client("client-linux.py")
        calls = []
        outputs = {"ss -t|wc -l": b"11\n", "ss -u|wc -l": b"6\n",
                   "ps -ef|wc -l": b"21\n", "ps -eLf|wc -l": b"41\n"}

        def fake_check_output(cmd, **kwargs):
            calls.append(cmd)
            return outputs[cmd]

        with mock.patch.object(client["os"], "listdir", side_effect=FileNotFoundError), \
                mock.patch.object(client["subprocess"], "check_output", side_effect=fake_check_output):
            self.assertEqual(client["tupd"](), (10, 5, 19, 39))
        self.assertEqual(calls, ["ss -t|wc -l", "ss -u|wc -l", "ps -ef|wc -l", "ps -eLf|wc -l"])

    def test_linux_tupd_hot_path_avoids_subprocess(self):
        """护栏：tupd 正常路径不得再起子进程。这四条命令每秒 12 次 fork/exec，
        子进程 CPU 实测 30~115ms/s（客户端自身看不到，只在 RUSAGE_CHILDREN 里）。"""
        source = (CLIENT_DIR / "client-linux.py").read_text(encoding="utf-8")
        self.assertIn("_count_sockets(('/proc/net/tcp', '/proc/net/tcp6')", source)
        self.assertIn("_count_sockets(('/proc/net/udp', '/proc/net/udp6'), keep=('01',))", source)
        self.assertIn("return _tupd_subprocess()", source)
        # 计数口径：ss -t 默认排除的三态
        self.assertIn("_TCP_STATE_EXCLUDE = ('03', '06', '0A')", source)

    def test_linux_tupd_matches_subprocess_counts(self):
        """真机对账：直读 /proc 与 ss/ps 的计数必须接近。连接数/进程数在两次调用之间
        会抖动（docker 容器起落），留 5% 或 5 的余量。"""
        if not sys.platform.startswith("linux"):
            return
        client = load_client("client-linux.py")
        native = client["tupd"]()
        legacy = client["_tupd_subprocess"]()
        for name, a, b in zip(("tcp", "udp", "process", "thread"), native, legacy):
            self.assertLessEqual(abs(a - b), max(5, b // 20), name)

    # --- Linux _disk_io（单轮扫描 + comm 缓存）----------------------------

    def test_linux_disk_io_diffs_consecutive_rounds(self):
        """单轮化：每轮只扫一遍 /proc，与上一轮快照求差（旧实现同一轮扫两遍）。
        只累加同名且计数不回退的 pid 差值、排除 bash；comm 走缓存，仅新 pid / 计数回退时重读。"""
        client = load_client("client-linux.py")
        io_rounds = {
            "100": [(1000, 2000), (1500, 2600)],   # 稳定增长 → 计 500/600
            "200": [(10, 20), (30, 40)],           # bash → 两轮都不计
            "300": [(500, 600), (5, 6)],           # 计数回退 → pid 复用，本轮不计
            "400": [(0, 0), (50, 60)],             # 第二轮才出现 → 无上一轮快照，不计
        }
        names = {"100": "worker", "200": "bash", "300": "worker", "400": "newcomer"}
        pids_rounds = [["100", "200", "300", "net"],
                       ["100", "200", "300", "400", "self"]]
        opened = []
        round_idx = [0]

        def fake_open(path, *args, **kwargs):
            opened.append(path)
            _, _, pid, kind = path.split("/")
            if kind == "io":
                read, write = io_rounds[pid][round_idx[0]]
                return io.StringIO("rchar: 0\nwchar: 0\nread_bytes: %d\nwrite_bytes: %d\n"
                                   "cancelled_write_bytes: 0\n" % (read, write))
            return io.StringIO(names[pid] + "\n")

        def fake_sleep(_seconds):
            round_idx[0] += 1
            if round_idx[0] >= len(pids_rounds):
                raise _LoopStop

        with mock.patch("builtins.open", side_effect=fake_open), \
                mock.patch.object(client["os"], "listdir",
                                  side_effect=lambda _p: pids_rounds[round_idx[0]]):
            run_loop_with_fake_sleep(client["_disk_io"], fake_sleep)

        self.assertEqual(client["diskIO"], {"read": 500, "write": 600})
        # comm 只在新 pid（首轮）与计数回退时读；稳定 pid 第二轮命中缓存
        self.assertEqual(opened.count("/proc/100/comm"), 1)
        self.assertEqual(opened.count("/proc/200/comm"), 1)
        self.assertEqual(opened.count("/proc/300/comm"), 2)
        self.assertEqual(opened.count("/proc/400/comm"), 1)

    def test_linux_disk_io_survives_transient_error(self):
        """单轮扫描抛异常（如 /proc 瞬时不可读）不得让线程退出：保留上一轮的值与快照，
        下一轮继续——差值自然跨越失败的那一轮，而不是整条线程消失。"""
        client = load_client("client-linux.py")
        io_rounds = {"100": [(100, 200), None, (300, 400)]}
        opened = []
        round_idx = [0]

        def fake_open(path, *args, **kwargs):
            opened.append(path)
            _, _, pid, kind = path.split("/")
            if kind == "io":
                read, write = io_rounds[pid][round_idx[0]]
                return io.StringIO("read_bytes: %d\nwrite_bytes: %d\n" % (read, write))
            return io.StringIO("worker\n")

        def fake_listdir(_path):
            if round_idx[0] == 1:
                raise OSError("proc busy")
            return ["100", "net"]

        def fake_sleep(_seconds):
            round_idx[0] += 1
            if round_idx[0] >= len(io_rounds["100"]):
                raise _LoopStop

        with mock.patch("builtins.open", side_effect=fake_open), \
                mock.patch.object(client["os"], "listdir", side_effect=fake_listdir):
            run_loop_with_fake_sleep(client["_disk_io"], fake_sleep)

        # 第 1 轮失败被跳过，第 2 轮与第 0 轮的快照求差（跨越失败轮）
        self.assertEqual(client["diskIO"], {"read": 200, "write": 200})
        self.assertEqual(opened.count("/proc/100/comm"), 1)

    # --- Windows 专项优化 -------------------------------------------------

    def test_windows_virtual_nic_fallback_is_cached(self):
        """兜底判定（net_if_stats）结果必须写回映射表：主循环每秒对每个网卡都调一次，
        不缓存会每秒重复枚举全部网卡（实测单次约 25ms）。"""
        client = load_client("client-psutil.py")
        client_globals = client["is_virtual_nic"].__globals__
        stats_calls = []

        def fake_stats():
            stats_calls.append(1)
            return {"奇怪网卡": types.SimpleNamespace(speed=0)}

        with mock.patch.object(client["sys"], "platform", "win"), \
                mock.patch.object(client["psutil"], "net_if_stats", side_effect=fake_stats), \
                mock.patch.dict(client_globals, {"_win_physical": {}, "_win_cache_clock": time.time(),
                                                 "_win_build_map": lambda: {}}):
            results = [client["is_virtual_nic"]("奇怪网卡") for _ in range(5)]

        self.assertEqual(results, [True] * 5)
        self.assertEqual(len(stats_calls), 1)

    def test_windows_nic_map_refresh_is_async(self):
        """映射过期后必须异步重建：PowerShell 单次 1.5~2s，同步做会把 net 监控线程
        整段堵住（期间不采样网卡计数）。"""
        client = load_client("client-psutil.py")
        client_globals = client["is_virtual_nic"].__globals__
        started = threading.Event()

        def slow_build():
            started.set()
            time.sleep(0.3)
            return {"网卡A": True}

        with mock.patch.object(client["sys"], "platform", "win"), \
                mock.patch.dict(client_globals, {"_win_physical": {"网卡A": True},
                                                 "_win_cache_clock": 0.0,
                                                 "_win_map_refreshing": False,
                                                 "_win_build_map": slow_build,
                                                 "NIC_MAP_REFRESH_INTERVAL": 1}):
            begin = time.perf_counter()
            result = client["is_virtual_nic"]("网卡A")
            elapsed = time.perf_counter() - begin
            # 必须趁 mock 生效期间等：撤销后后台线程会走到真的 _win_build_map（PowerShell 1.5~2s）
            rebuilt = started.wait(2)

        self.assertFalse(result)          # 立即用旧映射给出结果
        self.assertLess(elapsed, 0.1)     # 没有被 0.3s 的重建阻塞
        self.assertTrue(rebuilt)          # 后台确实在重建

    def test_windows_swap_uses_native_path(self):
        """Windows 的 get_swap 必须走 _win_swap（GlobalMemoryStatusEx + 常驻 PDH），
        而非 psutil.swap_memory——后者每次重开 PDH 查询并重新解析计数器路径，
        实测稳态 3.9ms、首次可达数百毫秒。"""
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        self.assertIn("r = _win_swap()", source)
        self.assertIn("Paging File(_Total)", source)
        self.assertIn("GlobalMemoryStatusEx", source)
        if sys.platform.startswith("win"):
            total, used = load_client("client-psutil.py")["get_swap"]()
            self.assertIsInstance(total, int)
            self.assertIsInstance(used, int)
            self.assertGreaterEqual(total, 0)
            self.assertGreaterEqual(used, 0)

    def test_windows_memory_uses_native_path(self):
        """Windows 的 get_memory 首选 ntdll 原生路径：GetPerformanceInfo 实测约 1.2ms
        （正常应约 0.05ms，慢在 psapi 那一层），原生等价路径约 0.005ms。三个量取自
        GlobalMemoryStatusEx + NtQuerySystemInformation(2/21)，偏移未文档化，故首次调用
        必须与 GetPerformanceInfo 对账后才启用。"""
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        self.assertIn("r = _win_mem_no_cache()", source)
        self.assertIn("_win_mem_gpi()", source)
        self.assertIn("_win_mem_native_ok", source)
        self.assertIn("_WIN_PERF_AVAIL_OFF = 0x2C", source)   # 布局护栏
        self.assertIn("_WIN_CACHE_SIZE_OFF = 0x28", source)
        if sys.platform.startswith("win"):
            client = load_client("client-psutil.py")
            # runpy.run_path 返回 globals 的拷贝，函数写回的是原始 dict，须经 __globals__ 读
            g = client["_win_mem_no_cache"].__globals__
            total, used = client["get_memory"]()
            self.assertIsInstance(total, int)
            self.assertIsInstance(used, int)
            self.assertGreater(total, 0)
            self.assertGreaterEqual(used, 0)
            self.assertLessEqual(used, total)
            self.assertTrue(g["_win_mem_native_ok"])          # 对账通过 → 走原生路径
            native = client["_win_mem_native"]()
            gpi = client["_win_mem_gpi"]()
            self.assertIsNotNone(native)
            self.assertIsNotNone(gpi)
            # 两路相邻读取之间内存会小幅抖动，容差与自检一致（64MB 或总量的 1%）
            self.assertLess(abs(native[1] - gpi[1]), max(65536, gpi[0] // 100))

    def test_windows_memory_falls_back_when_layout_drifts(self):
        """原生结构偏移未文档化：首次对账不通过时必须永久退回 GetPerformanceInfo，
        且不再重复走原生（否则每秒白付一次对账成本）。"""
        client = load_client("client-psutil.py")
        g = client["_win_mem_no_cache"].__globals__
        native_calls = []

        with mock.patch.dict(g, {"_win_mem_native": lambda: native_calls.append(1) or (100, 999999),
                                 "_win_mem_gpi": lambda: (100, 50),
                                 "_win_mem_native_ok": None}):
            first = client["_win_mem_no_cache"]()
            ok_after_first = g["_win_mem_native_ok"]
            client["_win_mem_no_cache"]()
            client["_win_mem_no_cache"]()
            ok_after_third = g["_win_mem_native_ok"]

        self.assertEqual(first, (100, 50))        # 退回到 GPI 的值
        self.assertFalse(ok_after_first)
        self.assertFalse(ok_after_third)
        self.assertEqual(len(native_calls), 1)    # 只对账一次，之后不再碰原生

    def test_windows_net_io_uses_native_path(self):
        """Windows 的网卡计数走原生路径：GetIfTable2 一次调用 + GetAdaptersAddresses 适配器名
        集合筛选。筛选是必须的——GetIfTable2 会列出 WFP/Npcap/QoS/VirtualBox 等**过滤层接口**，
        它们的计数与父网卡完全相同（同一批包被记多遍），全加会让上报流量翻数倍。
        实测 27.5ms → 1.07ms（13 网卡机）/ 7.3ms → 0.58ms（6 网卡机）。"""
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        self.assertIn("_win_net_io_native()", source)
        self.assertIn("GetIfTable2", source)
        self.assertIn("GetAdaptersAddresses", source)
        self.assertIn("_WIN_IF_ROW_SIZE = 1352", source)          # 布局护栏
        self.assertIn("_WIN_IF_IN_OFF = 1208", source)
        self.assertIn("_WIN_IF_OUT_OFF = 1280", source)
        self.assertIn("_WIN_GAA_FLAGS = 0x02 | 0x04 | 0x08", source)   # 不能带 INCLUDE_ALL_INTERFACES
        if sys.platform.startswith("win"):
            client = load_client("client-psutil.py")
            g = client["_net_io_counters"].__globals__
            native = client["_net_io_counters"]()
            self.assertTrue(g["_win_net_io_ok"])                  # 对账通过 → 走原生
            ps = client["_psutil_net_io"]()
            self.assertEqual(set(native), set(ps))                # 键集必须与 psutil 完全一致
            # 数值逐网卡一致（两次读取之间计数会前进，留 1% 余量）
            for name, stats in ps.items():
                for i in (0, 1):
                    self.assertLess(abs(native[name][i] - stats[i]),
                                    max(2000000, stats[i] // 100))

    def test_windows_net_io_falls_back_when_names_differ(self):
        """适配器名集合与 psutil 不符时必须永久退回 psutil——集合错了会把过滤层接口也算进去，
        上报流量直接翻倍。这是本项改动唯一会"算错数"的风险点。"""
        client = load_client("client-psutil.py")
        g = client["_net_io_counters"].__globals__
        if not sys.platform.startswith("win"):
            return
        with mock.patch.dict(g, {"_win_net_io_native": lambda: {"假网卡": (1, 2)},
                                 "_win_net_io_ok": None}):
            first = client["_net_io_counters"]()
            ok_after_first = g["_win_net_io_ok"]
            again = client["_net_io_counters"]()
            ok_after_second = g["_win_net_io_ok"]
        self.assertFalse(ok_after_first)
        self.assertFalse(ok_after_second)
        self.assertEqual(set(first), set(client["_psutil_net_io"]()))   # 退回 psutil 的值
        self.assertEqual(set(again), set(first))

    def test_windows_tcp_count_uses_native_table(self):
        """TCP 计数走 GetExtendedTcpTable 只读 dwNumEntries，避免 psutil 为每条连接
        构造对象（下载类机器上万连接时可达 70ms+）。"""
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        self.assertIn("_win_tcp_conn_count()", source)
        self.assertIn("GetExtendedTcpTable", source)
        if sys.platform.startswith("win"):
            client = load_client("client-psutil.py")
            native = client["_win_tcp_conn_count"]()
            self.assertIsNotNone(native)
            self.assertGreaterEqual(native, 0)
            # 连接表是活的，两次读取之间可能有极小抖动
            psutil_count = len(client["psutil"].net_connections(kind='tcp'))
            self.assertLessEqual(abs(native - psutil_count), 5)

    def test_windows_load_shares_tupd_snapshot(self):
        """就绪队列长度搭 tupd 那次原生快照的车，不再单独走 PDH 的 System provider
        （单次 4~9ms，是 Paging File provider 的两个数量级）。就绪队列 = ThreadState==Ready
        的线程数（实测与 PDH \\System\\Processor Queue Length 对齐：施压时 40.01 vs 39.86、
        min/max 一致），状态值只占低字节，按结构步长切片计数（纯 C 层，比逐线程解包快 3 倍）。"""
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        self.assertNotIn("_win_load_thread", source)
        self.assertNotIn("_win_pdh_queue_length", source)
        self.assertIn("_win_load_step(_win_ready_last)", source)
        if sys.platform.startswith("win"):
            client = load_client("client-psutil.py")
            # runpy.run_path 返回 globals 的拷贝，函数写回的是原始 dict，须经 __globals__ 读
            g = client["_win_sys_counts"].__globals__
            procs, threads, ready = client["_win_sys_counts"]()
            self.assertGreater(procs, 0)
            self.assertGreater(threads, 0)
            self.assertGreaterEqual(ready, 0)
            self.assertEqual(g["_win_ready_last"], ready)

            # 与 psutil 对账线程总数：两次快照之间有进程起落，留 2% 余量
            ps_threads = sum(p.num_threads() for p in client["psutil"].process_iter())
            self.assertLess(abs(threads - ps_threads), max(10, int(ps_threads * 0.02)))

            # 切片计数必须与逐线程解包在同一份缓冲上逐条对齐（偏移写死，兼作布局护栏）
            b = g["_win_sys_buf"]
            ref = 0
            off = 0
            while off < len(b):
                nxt = int.from_bytes(b[off:off + 4], "little")
                nthr = int.from_bytes(b[off + 4:off + 8], "little")
                limit = max(0, (len(b) - off - 0x100) // 80)
                for i in range(min(nthr, limit)):
                    p = off + 0x100 + i * 80 + 68
                    if int.from_bytes(b[p:p + 4], "little") == 1:
                        ref += 1
                if nxt == 0:
                    break
                off += nxt
            self.assertEqual(ready, ref)

    def test_all_client_threads_are_named(self):
        """每个线程函数入口都要调 _name_current_thread；且它必须同时写 Python 名与
        Windows 原生线程描述——Python 的 Thread(name=) 不写原生描述，Process Explorer
        与按 tid 查名的诊断脚本会看不到。"""
        client = load_client("client-psutil.py")
        source = (CLIENT_DIR / "client-psutil.py").read_text(encoding="utf-8")
        for marker in ("'net-monitor'", "'net-probe'", "'tupd'",
                       "'disk-io'", "'nic-map-refresh'", "'ping-'", "'monitor-'"):
            self.assertIn("_name_current_thread(" + marker, source, marker)

        client["_name_current_thread"]("unit-named-thread")
        self.assertEqual(threading.current_thread().name, "unit-named-thread")
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.windll.kernel32
            k32.GetThreadDescription.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
            k32.GetThreadDescription.restype = ctypes.c_long
            buf = ctypes.c_wchar_p()
            k32.GetThreadDescription(k32.GetCurrentThread(), ctypes.byref(buf))
            self.assertEqual(buf.value, "unit-named-thread")


if __name__ == "__main__":
    unittest.main()
