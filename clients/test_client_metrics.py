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


if __name__ == "__main__":
    unittest.main()
