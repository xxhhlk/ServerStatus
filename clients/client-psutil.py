#!/usr/bin/env python3
# coding: utf-8
# Update by : https://github.com/cppla/ServerStatus, Update date: 20250902
# 依赖于psutil跨平台库
# 版本：1.1.0, 支持Python版本：3.6+
# 支持操作系统： Linux, Windows, OSX, Sun Solaris, FreeBSD, OpenBSD and NetBSD, both 32-bit and 64-bit architectures
# 说明: 默认情况下修改server和user就可以了。丢包率监测方向可以自定义，例如：CU = "www.facebook.com"。

SERVER = ""
USER = ""


PASSWORD = "USER_DEFAULT_PASSWORD"
PORT = 35601
CU = "cu.tz.cloudcpp.com"
CT = "ct.tz.cloudcpp.com"
CM = "cm.tz.cloudcpp.com"
PROBEPORT = 80
PROBE_PROTOCOL_PREFER = "ipv4"  # ipv4, ipv6
PING_PACKET_HISTORY_LEN = 100
INTERVAL = 1
# 三网探针域名 TTL 约 180s，30s 已能拿到绝大部分削减收益；
# 勿低于 10s，否则相对 TTL 仍是重复解析。
DNS_REFRESH_INTERVAL = 30  # 重解析探针域名的间隔（秒），不影响 1s 建连探测频率
NET_PROBE_INTERVAL = 30    # online4/online6 探测间隔（秒）
TUP_INTERVAL = 3           # tcp/udp/process/thread 采样间隔（秒）

import socket
import time
import timeit
import os
import sys
import subprocess
import json
import errno
import math
import ctypes
from ctypes import wintypes
import psutil
import threading
import platform
from queue import Queue

def _env_str(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value

def _env_int(name, default):
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default

# Allow docker env overrides. 优先级：运行程序传递参数 > 用户修改的USER > Docker/系统
# 环境变量统一加 serverstatus_ 前缀（如 serverstatus_SERVER、serverstatus_PASSWORD）
SERVER = _env_str("serverstatus_SERVER", SERVER) if SERVER == "" else SERVER
USER = _env_str("serverstatus_USER", USER) if USER == "" else USER
PASSWORD = _env_str("serverstatus_PASSWORD", PASSWORD)
PORT = _env_int("serverstatus_PORT", PORT)
INTERVAL = _env_int("serverstatus_INTERVAL", INTERVAL)
PROBEPORT = _env_int("serverstatus_PROBEPORT", PROBEPORT)
PROBE_PROTOCOL_PREFER = _env_str("serverstatus_PROBE_PROTOCOL_PREFER", PROBE_PROTOCOL_PREFER)
PING_PACKET_HISTORY_LEN = _env_int("serverstatus_PING_PACKET_HISTORY_LEN", PING_PACKET_HISTORY_LEN)
DNS_REFRESH_INTERVAL = _env_int("serverstatus_DNS_REFRESH_INTERVAL", DNS_REFRESH_INTERVAL)
NET_PROBE_INTERVAL = _env_int("serverstatus_NET_PROBE_INTERVAL", NET_PROBE_INTERVAL)
TUP_INTERVAL = _env_int("serverstatus_TUP_INTERVAL", TUP_INTERVAL)
CU = _env_str("serverstatus_CU", CU)
CT = _env_str("serverstatus_CT", CT)
CM = _env_str("serverstatus_CM", CM)

def parse_cli_args(arguments):
    overrides = {}
    for argument in arguments:
        key, separator, value = argument.partition('=')
        if separator and key in {'SERVER', 'PORT', 'USER', 'PASSWORD', 'INTERVAL'}:
            overrides[key] = value
    return overrides

def _name_current_thread(name):
    """给当前线程命名：设 Python 名，并在 Windows 上补写原生线程描述。
    Python 的 Thread(name=) 不会写原生描述（实测读回来为空），而 Process Explorer
    以及按 tid 查名的诊断脚本都依赖它，所以这里补一次 SetThreadDescription。"""
    threading.current_thread().name = name
    if not sys.platform.startswith('win'):
        return
    try:
        fn = getattr(_name_current_thread, 'setdesc', None)
        if fn is None:
            k32 = ctypes.windll.kernel32
            fn = k32.SetThreadDescription
            fn.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR]
            fn.restype = ctypes.c_long
            _name_current_thread.setdesc = fn
            _name_current_thread.getcur = k32.GetCurrentThread
        fn(_name_current_thread.getcur(), name)
    except Exception:
        _name_current_thread.setdesc = None   # 绑定失败下次重试

_boot_time = None

def get_uptime():
    global _boot_time
    if _boot_time is None:
        _boot_time = psutil.boot_time()
    return int(time.time() - _boot_time)

def _win_mem_gpi():
    """GetPerformanceInfo：一次调用拿 total/free/SystemCache，used 排除系统缓存（近似 Linux cached 语义）。
    返回 (total_kb, used_kb)；失败返回 None。"""
    import ctypes
    from ctypes import wintypes
    class PERF_INFO(ctypes.Structure):
        _fields_ = [
            ('cb', wintypes.DWORD), ('CommitTotal', ctypes.c_size_t),
            ('CommitLimit', ctypes.c_size_t), ('CommitPeak', ctypes.c_size_t),
            ('PhysicalTotal', ctypes.c_size_t), ('PhysicalAvailable', ctypes.c_size_t),
            ('SystemCache', ctypes.c_size_t), ('KernelTotal', ctypes.c_size_t),
            ('KernelPaged', ctypes.c_size_t), ('KernelNonpaged', ctypes.c_size_t),
            ('PageSize', ctypes.c_size_t), ('HandleCount', wintypes.DWORD),
            ('ProcessCount', wintypes.DWORD), ('ThreadCount', wintypes.DWORD),
        ]
    pi = PERF_INFO()
    pi.cb = ctypes.sizeof(PERF_INFO)
    if not ctypes.windll.psapi.GetPerformanceInfo(ctypes.byref(pi), pi.cb):
        return None
    page = pi.PageSize
    total = pi.PhysicalTotal * page
    free = pi.PhysicalAvailable * page
    cache = pi.SystemCache * page        # standby + modified + 活动映射
    return int(total/1024.0), int((total - free - cache)/1024.0)

# NtQuerySystemInformation 的信息类与字段偏移（x64，未文档化；取值均与 GetPerformanceInfo 同源同值）
_WIN_PERF_AVAIL_OFF = 0x2C      # class 2  SystemPerformanceInformation：AvailablePages
_WIN_CACHE_SIZE_OFF = 0x28      # class 21 SystemFileCacheInformation：当前文件缓存页数

_win_mem_buf = None             # NQSI(2)/NQSI(21) 共用的常驻缓冲
_win_mem_native_ok = None       # None 未自检 / True 可用 / False 永久退回 GetPerformanceInfo

def _win_page_size():
    """系统页大小（字节），GetSystemInfo 惰性调用一次"""
    v = getattr(_win_page_size, 'v', None)
    if v is None:
        buf = ctypes.create_string_buffer(64)
        ctypes.windll.kernel32.GetSystemInfo(ctypes.byref(buf))
        v = int.from_bytes(buf.raw[4:8], 'little') or 4096   # SYSTEM_INFO.dwPageSize
        _win_page_size.v = v
    return v

def _win_mem_native():
    """ntdll 原生路径：GlobalMemoryStatusEx 取物理内存总量，NQSI(2)/NQSI(21) 取可用页数与
    系统缓存页数。三个量都与 GetPerformanceInfo 逐次精确一致，但绕开了 psapi 那一层
    （实测该调用被第三方组件挂钩后单次 3~8ms，正常应约 0.05ms）。返回 (total_kb, used_kb)；
    任一步失败返回 None。"""
    global _win_mem_buf
    nqsi = _win_nqsi()
    ms = _win_mem_status()
    if ms is None:
        return None
    if _win_mem_buf is None:
        _win_mem_buf = ctypes.create_string_buffer(4096)
    buf, ret = _win_mem_buf, wintypes.ULONG(0)
    if nqsi(2, buf, len(buf), ctypes.byref(ret)) != 0:
        return None
    free_pages = int.from_bytes(buf.raw[_WIN_PERF_AVAIL_OFF:_WIN_PERF_AVAIL_OFF + 4], 'little')
    if nqsi(21, buf, len(buf), ctypes.byref(ret)) != 0:
        return None
    cache_pages = int.from_bytes(buf.raw[_WIN_CACHE_SIZE_OFF:_WIN_CACHE_SIZE_OFF + 8], 'little')
    page = _win_page_size()
    total = ms.ullTotalPhys
    return int(total/1024.0), int((total - free_pages * page - cache_pages * page)/1024.0)

def _win_mem_no_cache():
    """排除系统缓存的物理内存占用，返回 (total_kb, used_kb)；失败返回 None。

    首选原生路径（约 0.005ms，比 GetPerformanceInfo 快约 700 倍）。原生结构偏移未文档化，
    故首次调用与 GetPerformanceInfo 对账，偏差超过总量 1% 即永久退回后者，避免随系统版本漂移出错。"""
    global _win_mem_native_ok
    if _win_mem_native_ok is not False:
        r = _win_mem_native()
        if r is not None:
            if _win_mem_native_ok is None:
                g = _win_mem_gpi()
                if g is None:
                    _win_mem_native_ok = True
                else:
                    tol = max(65536, g[0] // 100)     # 64MB 或总量的 1%
                    _win_mem_native_ok = abs(r[0] - g[0]) <= tol and abs(r[1] - g[1]) <= tol
                    if not _win_mem_native_ok:
                        return g
            return r
        _win_mem_native_ok = False
    return _win_mem_gpi()

def get_memory():
    if sys.platform.startswith("win"):
        r = _win_mem_no_cache()
        if r:
            return r
    Mem = psutil.virtual_memory()
    return int(Mem.total / 1024.0), int(Mem.used / 1024.0)

def get_swap():
    if sys.platform.startswith("win"):
        r = _win_swap()
        if r is not None:
            return r
    Mem = psutil.swap_memory()
    return int(Mem.total/1024.0), int(Mem.used/1024.0)

_hdd_mounts = None
_hdd_mounts_clock = 0.0
HDD_REFRESH_INTERVAL = 60  # 分区表重枚举间隔（秒），保证热插盘也能被发现

def get_hdd():
    global _hdd_mounts, _hdd_mounts_clock
    if "darwin" in sys.platform:
        return int(psutil.disk_usage("/").total/1024.0/1024.0), int((psutil.disk_usage("/").total-psutil.disk_usage("/").free)/1024.0/1024.0)
    elif sys.platform.startswith("win"):
        # Windows：只统计系统盘（SystemDrive，通常 C:），避免多盘/外置盘混入
        sysdrive = os.environ.get("SystemDrive", "C:") + os.sep
        usage = psutil.disk_usage(sysdrive)
        return int(usage.total/1024.0/1024.0), int(usage.used/1024.0/1024.0)
    else:
        now = time.monotonic()
        if _hdd_mounts is None or now - _hdd_mounts_clock > HDD_REFRESH_INTERVAL:
            valid_fs = ["ext4", "ext3", "ext2", "reiserfs", "jfs", "btrfs", "fuseblk", "zfs", "simfs", "ntfs", "fat32",
                        "exfat", "xfs"]
            disks = dict()
            for disk in psutil.disk_partitions():
                if not disk.device in disks and disk.fstype.lower() in valid_fs:
                    disks[disk.device] = disk.mountpoint
            _hdd_mounts = list(disks.values())
            _hdd_mounts_clock = now
        size = 0
        used = 0
        for disk in _hdd_mounts:
            usage = psutil.disk_usage(disk)
            size += usage.total
            used += usage.used
        return int(size/1024.0/1024.0), int(used/1024.0/1024.0)

def get_cpu():
    return psutil.cpu_percent(interval=INTERVAL)

def get_cpu_cores():
    return psutil.cpu_count(logical=True) or 0

def normalize_cpu_model(value):
    return " ".join(str(value or "").split())[:160]

def is_generic_cpu_model(value):
    v = normalize_cpu_model(value).lower().replace('-', '').replace('_', '').replace(' ', '')
    return v in ('', 'unknown', 'x8664', 'amd64', 'i386', 'i686', 'aarch64', 'arm64') or v.startswith('armv')

def get_platform_cpu_vendor():
    values = [
        platform.processor(),
        getattr(platform.uname(), 'processor', ''),
        platform.machine(),
        getattr(platform.uname(), 'machine', ''),
        platform.platform(),
    ]
    text = " ".join(normalize_cpu_model(v).lower() for v in values)
    if 'genuineintel' in text:
        return 'GenuineIntel'
    if 'authenticamd' in text:
        return 'AuthenticAMD'
    if 'intel' in text:
        return 'Intel'
    if 'amd' in text:
        return 'AMD'
    if sys.platform.startswith('darwin') and platform.machine().lower() in ('arm64', 'aarch64'):
        return 'Apple'
    if any(token in text for token in ('aarch64', 'arm64', 'armv7', 'armv8', ' arm ')):
        return 'ARM'
    return ''

def get_platform_cpu_arch():
    return normalize_cpu_model(platform.machine() or platform.processor() or platform.architecture()[0])

def _win_cpu_model():
    """Windows: 注册表 ProcessorNameString 为标准品牌名（platform.processor() 只是 Family/Model 编码）"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                             r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
        try:
            val, _ = winreg.QueryValueEx(key, "ProcessorNameString")
            return normalize_cpu_model(val)
        finally:
            winreg.CloseKey(key)
    except Exception:
        return None

def get_cpu_model():
    if sys.platform.startswith('win'):
        m = _win_cpu_model()
        if m:
            return m
    for value in (platform.processor(), getattr(platform.uname(), 'processor', '')):
        value = normalize_cpu_model(value)
        if value and not is_generic_cpu_model(value):
            return value
    vendor = normalize_cpu_model(get_platform_cpu_vendor())
    if vendor:
        return vendor
    return get_platform_cpu_arch()

# --- 虚拟网卡识别（非 MAC 方案） ---
# Windows: WMI PNPDeviceID 前缀 PCI\ / USB\ = 物理；其余（ROOT\ / SWD\ / BTH\ / {GUID}）= 虚拟。
#          VM 内主网卡（VEN_80EE / 1AF4 / 15AD）也是 PCI\，不会被误伤。
# Linux:   /sys/class/net/<if>/device 软链接不存在 → 非物理（lo/docker0/veth/tun/br-）。
_win_physical = None
_win_cache_clock = 0.0
_win_map_refreshing = False
NIC_MAP_REFRESH_INTERVAL = 1800   # PNPDeviceID 映射重建间隔（秒）
_WIN_NAME_HINTS = ('Loopback', 'Wi-Fi Direct', 'WAN Miniport', 'Bluetooth',
                   'Apple Mobile', 'Kernel Debug', 'TAP-', 'OpenVPN',
                   'Tailscale', 'WireGuard', 'VMware', 'VirtualBox',
                   'Host-Only', 'vEthernet', 'Default Switch')

def _win_is_physical(pnp_id):
    """PCI 或 USB 开头 → 物理网卡"""
    return bool(pnp_id) and pnp_id.startswith(('PCI\\', 'USB\\'))

def _win_build_map():
    """NetConnectionID → 是否物理"""
    ps = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
          "Get-CimInstance Win32_NetworkAdapter | "
          "Where-Object { $_.NetConnectionID } | "
          "Select-Object NetConnectionID, PNPDeviceID | ConvertTo-Json -Compress")
    out = subprocess.run(['powershell', '-NoProfile', '-Command', ps],
                         capture_output=True, text=True, encoding='utf-8',
                         errors='replace', timeout=10).stdout
    items = json.loads(out)
    if isinstance(items, dict):
        items = [items]
    return {it['NetConnectionID']: _win_is_physical(it['PNPDeviceID']) for it in items}

def _win_refresh_map():
    """后台重建 PNPDeviceID 映射。PowerShell 单次要 1.5~2s，若放在 net 监控线程里同步做，
    会把它整段堵住（期间不采样网卡计数），所以过期后异步刷新、期间继续用旧映射。"""
    _name_current_thread('nic-map-refresh')
    global _win_physical, _win_cache_clock, _win_map_refreshing
    try:
        mapping = _win_build_map()
        if mapping:
            _win_physical = mapping
    except Exception:
        pass
    _win_cache_clock = time.time()   # 失败也推后重试，避免每轮都起 PowerShell
    _win_map_refreshing = False

def is_virtual_nic(name):
    """排除虚拟网卡和回环接口"""
    if sys.platform.startswith('win'):
        global _win_physical, _win_cache_clock, _win_map_refreshing
        now = time.time()
        if _win_physical is None:
            # 首次必须同步建：没有映射就无法判断任何网卡（仅启动时一次）
            try:
                _win_physical = _win_build_map()
            except Exception:
                _win_physical = {}   # 失败时靠名字提示与 net_if_stats 兜底，宁可多统计不崩
            _win_cache_clock = time.time()
        elif now - _win_cache_clock > NIC_MAP_REFRESH_INTERVAL and not _win_map_refreshing:
            _win_map_refreshing = True
            threading.Thread(target=_win_refresh_map, daemon=True).start()
        if name in _win_physical:
            return not _win_physical[name]
        if any(h in name for h in _WIN_NAME_HINTS):
            return True
        try:
            virtual = psutil.net_if_stats()[name].speed == 0
        except Exception:
            virtual = False
        # 记住兜底结果：net_if_stats 要枚举全部网卡（实测单个约 25ms），
        # 而主循环每秒对每个网卡都调一次，不缓存会白白吃掉大量 CPU。
        _win_physical[name] = not virtual
        return virtual
    if name == 'lo':
        return True
    if not os.access('/sys/class/net', os.R_OK):
        return False   # /sys 不可读（Android 非 root）→ 无法判断，宁可计入
    return not os.path.exists('/sys/class/net/%s/device' % name)

_net_in = 0
_net_out = 0
_net_lock = threading.Lock()

def _sum_physical_counters(net):
    """按 is_virtual_nic 过滤后累加 (total_in, total_out)。
    入参值一律按 (bytes_sent, bytes_recv) 序，即 bytes_recv 下标 1、bytes_sent 下标 0——
    psutil 的 namedtuple 与 _win_net_io_native 返回的元组都遵循这一顺序。
    抽成纯函数便于单测，不改变 _net_monitor 的单线程唯一调用设计。"""
    total_in = 0
    total_out = 0
    for name, stats in (net or {}).items():
        if is_virtual_nic(name):
            continue
        total_in += stats[1]
        total_out += stats[0]
    return total_in, total_out

def _psutil_net_io():
    """psutil 网卡计数（兜底）。全文件唯一调用点——_net_monitor 是唯一读者，
    并发读的隐患从根源上消除（护栏测试锁定这一点）。"""
    return psutil.net_io_counters(pernic=True)

_win_net_io_ok = None      # None 未自检 / True 原生可用 / False 永久退回 psutil

def _net_io_counters():
    """网卡计数入口。Windows 首选原生路径（GetIfTable2 + 适配器名集合筛选），键集与数值
    与 psutil 逐次一致但便宜得多（实测 27.5ms → 1.07ms、7.3ms → 0.58ms）；原生不可用或
    首次对账不通过则退回 psutil。"""
    global _win_net_io_ok
    if sys.platform.startswith("win"):
        if _win_net_io_ok is None:
            native = _win_net_io_selfcheck()
            _win_net_io_ok = native is not None
            if native is not None:
                return native
        elif _win_net_io_ok:
            native = _win_net_io_native()
            if native is not None:
                return native
            _win_net_io_ok = False
    return _psutil_net_io()

def _net_monitor():
    """独立线程：唯一读取网卡计数的地方，读累计值存全局并算网速"""
    _name_current_thread('net-monitor')
    global _net_in, _net_out
    while True:
        try:
            total_in, total_out = _sum_physical_counters(_net_io_counters())
            with _net_lock:
                _net_in = total_in
                _net_out = total_out
            # 用 update_net_speed 统一算速：time.monotonic 不受系统时钟回拨影响，
            # 且对计数器回绕（total < prev）置 0，避免负数/异常尖峰
            update_net_speed(total_in, total_out)
        except Exception:
            # 保留最后有效值，短暂等待后自动重试，避免线程永久退出
            pass
        time.sleep(INTERVAL)

_online_status = {4: False, 6: False}
_online_target = 4

def _net_probe_thread():
    """独立线程：每 NET_PROBE_INTERVAL 秒探测一次 online4/online6（get_network），
    避免 DNS 卡死阻塞主循环上报。默认 30s 对 600s 的 offline warning 看门狗足够。
    主循环只读 _online_status 快照；_online_target 由主循环在连接后设置。"""
    _name_current_thread('net-probe')
    global _online_status
    while True:
        target = _online_target
        try:
            _online_status[target] = get_network(target)
        except Exception:
            _online_status[target] = False
        time.sleep(NET_PROBE_INTERVAL)

_os_name = None

def get_os_name():
    global _os_name
    if _os_name is not None:
        return _os_name
    try:
        sysname = platform.system().lower()
        if sysname.startswith('windows'):
            _os_name = 'windows'
        elif sysname.startswith('darwin') or 'mac' in sysname:
            _os_name = 'darwin'
        elif 'bsd' in sysname:
            _os_name = 'bsd'
        elif sysname.startswith('linux'):
            _os_name = 'linux'
            try:
                with open('/etc/os-release') as f:
                    for line in f:
                        if line.startswith('ID='):
                            value = line.strip().split('=', 1)[1].strip().strip('"')
                            if value:
                                _os_name = value
                            break
            except Exception:
                pass
        else:
            _os_name = sysname or 'unknown'
    except Exception:
        return 'unknown'
    return _os_name

def liuliang():
    """主循环读取：只读独立线程存下的全局值，不调 psutil"""
    with _net_lock:
        return _net_in, _net_out

def _win_nqsi():
    """ntdll.NtQuerySystemInformation，惰性初始化一次（全部 Windows 原生采样共用）"""
    fn = getattr(_win_nqsi, 'fn', None)
    if fn is None:
        fn = ctypes.WinDLL('ntdll').NtQuerySystemInformation
        fn.argtypes = [wintypes.ULONG, wintypes.LPVOID, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        fn.restype = wintypes.LONG
        _win_nqsi.fn = fn
    return fn

# SYSTEM_PROCESS_INFORMATION / SYSTEM_THREAD_INFORMATION 的字段偏移（x64）
_WIN_THREADS_OFF = 0x100       # 进程条目内 Threads[] 数组起点
_WIN_THREAD_SIZE = 80          # 单个线程结构大小
_WIN_THREAD_STATE_OFF = 68     # 线程结构内 ThreadState（0 Initialized / 1 Ready / 2 Running …）

_win_sys_buf = None            # 常驻复用缓冲：每次新建 1MB 约多花 1ms
_win_ready_last = None         # 最近一次快照的就绪队列长度，交给 load 的 EWMA

def _win_sys_counts():
    """NtQuerySystemInformation(SystemProcessInformation)：一次系统调用拿全部进程数、线程数
    与就绪队列长度。与任务管理器同源，替代逐进程 psutil.Process().num_threads()（Windows 上极慢）。

    就绪队列长度 = ThreadState==Ready(1) 的线程数，与文档化 PDH 计数器
    \\System\\Processor Queue Length 等价（实测施压 16 进程时两者均值 40.01 vs 39.86、
    min/max 完全一致），但省掉 PDH 的 System provider——那个 provider 单次 4~9ms，
    是 Paging File provider 的两个数量级。状态值只占低字节，按结构步长切片一次取出
    整进程的状态字节再计数（纯 C 层，比逐线程解包快 3 倍）。

    失败返回 (0, 0, None)。"""
    global _win_sys_buf, _win_ready_last
    nqsi = _win_nqsi()
    buf_size = 1 << 20          # 1MB 起
    while True:
        if _win_sys_buf is None or len(_win_sys_buf) < buf_size:
            _win_sys_buf = ctypes.create_string_buffer(buf_size)
        ret_len = wintypes.ULONG(0)
        status = nqsi(5, _win_sys_buf, len(_win_sys_buf), ctypes.byref(ret_len))
        if status == 0:                                          # STATUS_SUCCESS
            break
        if status == 0xC0000004:                                 # STATUS_INFO_LENGTH_MISMATCH → 扩大重试
            buf_size = ret_len.value + 0x10000
            _win_sys_buf = None
            continue
        return 0, 0, None
    b = _win_sys_buf
    procs = threads = ready = off = 0
    while off < len(b):
        nxt = int.from_bytes(b[off:off+4], 'little')
        nthr = int.from_bytes(b[off+4:off+8], 'little')          # NumberOfThreads
        threads += nthr
        procs += 1
        start = off + _WIN_THREADS_OFF + _WIN_THREAD_STATE_OFF
        stop = start + nthr * _WIN_THREAD_SIZE
        if stop > len(b):
            stop = len(b)
        ready += b[start:stop:_WIN_THREAD_SIZE].count(1)
        if nxt == 0:
            break
        off += nxt
    _win_ready_last = ready
    return procs, threads, ready

# --- Windows 负载（load average 近似） ---
# 就绪队列长度取自 tupd 那次原生快照里 ThreadState==Ready 的线程数（与文档化 PDH 计数器
# \System\Processor Queue Length 等价，实测已对齐），因此不再单独走 PDH 的 System provider。
# 运行线程数用每核 CPU 利用率连续求和（单核利用率 60% 记 0.6，用 SystemProcessorPerformanceInformation 采样）。
# 1/5/15 分钟用内核同款指数衰减 EWMA：load = prev*exp(-dt/tau) + instant*(1-exp(-dt/tau))。
# 采样节奏由 tupd 线程按 TUP_INTERVAL 驱动——load 是长时间平滑量，粗采不影响读数。
_win_load_averages = [0.0, 0.0, 0.0]          # [1min, 5min, 15min]，主循环只读快照
_win_load_lock = threading.Lock()
_win_load_prev_idle = None
_win_load_prev_clock = 0.0
_win_load_warm_until = time.time() + 120

class _WIN_PERF_INFO(ctypes.Structure):
    """SYSTEM_PROCESSOR_PERFORMANCE_INFORMATION：每核 idle/kernel/user 时间，48 字节"""
    _fields_ = [
        ('IdleTime', ctypes.c_ulonglong),
        ('KernelTime', ctypes.c_ulonglong),
        ('UserTime', ctypes.c_ulonglong),
        ('DpcTime', ctypes.c_ulonglong),
        ('InterruptTime', ctypes.c_ulonglong),
        ('InterruptCount', ctypes.c_ulong),
    ]

def _win_perf_snapshot():
    """SystemProcessorPerformanceInformation (8)：每核 idle/kernel/user 时间。失败返回 None"""
    nqsi = _win_nqsi()
    nproc = psutil.cpu_count(logical=True) or 1
    perfs = (_WIN_PERF_INFO * nproc)()
    ret_len = wintypes.ULONG(0)
    if nqsi(8, perfs, ctypes.sizeof(perfs), ctypes.byref(ret_len)) != 0:
        return None
    return list(perfs[:ret_len.value // ctypes.sizeof(_WIN_PERF_INFO)])

def _win_load_step(qlen):
    """推进 1/5/15 分钟负载 EWMA。由 tupd 线程按 TUP_INTERVAL 驱动；
    qlen 是本次原生快照里的就绪队列长度（Ready 态线程数），与运行线程数相加得到瞬时负载。

    运行线程数 = Σ(每核 CPU 利用率)，连续值（单核跑满记 1、利用率 60% 记 0.6），
    避免离散阈值在中等负载区间低估。"""
    global _win_load_prev_idle, _win_load_prev_clock
    try:
        perfs = _win_perf_snapshot()
    except Exception:
        perfs = None
    if perfs is None and qlen is None:
        return
    running = 0.0
    if perfs is not None:
        if _win_load_prev_idle is not None:
            for cur, prev in zip(perfs, _win_load_prev_idle):
                # 注意：KernelTime 包含 IdleTime，必须先减掉再算内核忙碌时间
                idle = cur.IdleTime - prev.IdleTime
                busy = (cur.KernelTime - prev.KernelTime) - idle + (cur.UserTime - prev.UserTime)
                total = busy + idle
                if total > 0:
                    running += busy / total
        _win_load_prev_idle = perfs
    instant = (qlen or 0) + running
    now = time.time()
    with _win_load_lock:
        if now < _win_load_warm_until:
            # 启动后 2 分钟 warm-up：直接报瞬时值，避免数据未就绪/无基线时把起点钉在 0，
            # 导致监控读数从 0 缓慢爬升
            _win_load_averages[:] = [float(instant)] * 3
        elif _win_load_prev_clock > 0:
            dt = now - _win_load_prev_clock
            avg = list(_win_load_averages)
            for i, t in enumerate((60.0, 300.0, 900.0)):
                f = math.exp(-dt / t)
                avg[i] = avg[i] * f + instant * (1.0 - f)
            _win_load_averages[:] = avg
        else:
            _win_load_averages[:] = [float(instant)] * 3
    _win_load_prev_clock = now

class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]

class _PDH_FMT_DOUBLE(ctypes.Structure):
    _fields_ = [('CStatus', wintypes.LONG), ('value', ctypes.c_double)]

_swap_pdh = None
_swap_pdh_query = None
_swap_pdh_counter = None

def _win_mem_status():
    """GlobalMemoryStatusEx，惰性绑定并复用缓冲区；失败返回 None"""
    fn = getattr(_win_mem_status, 'fn', None)
    if fn is None:
        fn = ctypes.windll.kernel32.GlobalMemoryStatusEx
        fn.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
        fn.restype = wintypes.BOOL
        buf = _MEMORYSTATUSEX()
        buf.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        _win_mem_status.fn = fn
        _win_mem_status.buf = buf
    if not fn(ctypes.byref(_win_mem_status.buf)):
        return None
    return _win_mem_status.buf

def _win_swap_percent():
    """页面文件占用率（%）。PDH 查询惰性打开后常驻：psutil 每次调用都重开查询并重新解析
    计数器路径，首次可达数百毫秒；常驻后单次约 0.03ms。失败返回 None。"""
    global _swap_pdh, _swap_pdh_query, _swap_pdh_counter
    if _swap_pdh is None:
        try:
            pdh = ctypes.WinDLL('pdh')
            pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhOpenQueryW.restype = wintypes.LONG
            pdh.PdhAddEnglishCounterW.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, ctypes.c_size_t, ctypes.POINTER(wintypes.HANDLE)]
            pdh.PdhAddEnglishCounterW.restype = wintypes.LONG
            pdh.PdhCollectQueryData.argtypes = [wintypes.HANDLE]
            pdh.PdhCollectQueryData.restype = wintypes.LONG
            pdh.PdhGetFormattedCounterValue.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                        ctypes.POINTER(wintypes.DWORD),
                                                        ctypes.POINTER(_PDH_FMT_DOUBLE)]
            pdh.PdhGetFormattedCounterValue.restype = wintypes.LONG
            query = wintypes.HANDLE()
            counter = wintypes.HANDLE()
            if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
                return None
            if pdh.PdhAddEnglishCounterW(query, "\\Paging File(_Total)\\% Usage", 0, ctypes.byref(counter)) != 0:
                return None
            pdh.PdhCollectQueryData(query)
            _swap_pdh, _swap_pdh_query, _swap_pdh_counter = pdh, query, counter
        except Exception:
            return None
    try:
        if _swap_pdh.PdhCollectQueryData(_swap_pdh_query) != 0:
            return None
        value = _PDH_FMT_DOUBLE()
        ctype = wintypes.DWORD(0)
        # 0x200 = PDH_FMT_DOUBLE
        if _swap_pdh.PdhGetFormattedCounterValue(_swap_pdh_counter, 0x200, ctypes.byref(ctype), ctypes.byref(value)) != 0:
            return None
        if value.CStatus not in (0, 1):
            return None
        return value.value
    except Exception:
        return None

def _win_swap():
    """Windows swap，与 psutil.swap_memory() 同源同值：total 取页面文件总量
    （GlobalMemoryStatusEx），used = 占用率 × total。实测与 psutil 逐位一致，
    单次约 0.03ms（psutil 约 3.9ms，首次可达数百毫秒）。失败返回 None。"""
    ms = _win_mem_status()
    if ms is None:
        return None
    total = ms.ullTotalPageFile - ms.ullTotalPhys
    if total <= 0:
        return 0, 0
    percent = _win_swap_percent()
    if percent is None:
        return None
    return int(total / 1024.0), int(0.01 * percent * total / 1024.0)

# MIB_IF_ROW2（x64）字段偏移：行距 1352，表头 8 字节。标定方式为扫描缓冲里的宽字符串定行距、
# 再用 psutil 的值反解字段；两台真机（13 / 6 个网卡）逐网卡零差异。
_WIN_IF_ROW_SIZE = 1352
_WIN_IF_ALIAS_OFF = 28
_WIN_IF_IN_OFF = 1208
_WIN_IF_OUT_OFF = 1280

# GetAdaptersAddresses 标志：SKIP_ANYCAST | SKIP_MULTICAST | SKIP_DNS_SERVER。
# 不能带 INCLUDE_ALL_INTERFACES——那会把 WFP / Npcap / QoS / VirtualBox 等**过滤层接口**
# 也列出来，它们的计数与父网卡完全相同（同一批包被记多遍），全加会让上报流量翻数倍。
_WIN_GAA_FLAGS = 0x02 | 0x04 | 0x08

NET_NAMES_REFRESH_INTERVAL = 1800      # 适配器名集合刷新间隔（秒）

_win_gaa = None
_win_net_names = None
_win_net_names_clock = 0.0

def _win_wstr_at(raw, off, maxchars=257):
    """从字节串里解定长 UTF-16LE 字符串（遇 NUL 截断）"""
    end = off
    for k in range(maxchars):
        if raw[off + 2 * k] == 0 and raw[off + 2 * k + 1] == 0:
            break
        end = off + 2 * k + 2
    return raw[off:end].decode('utf-16le', 'replace')

def _win_gaa_names():
    """GetAdaptersAddresses 的 FriendlyName 集合。该集合与 psutil.net_io_counters 的键集
    实测逐次完全一致（13 / 6 个网卡零差异），是判断"哪些网卡该计入"的正确来源——
    光看 GetIfTable2 的 FilterInterface 标志不够（未连接的物理网卡、隧道、Wi-Fi Direct
    也要排除）。失败返回 None。"""
    global _win_gaa
    try:
        if _win_gaa is None:
            fn = ctypes.WinDLL('iphlpapi').GetAdaptersAddresses
            fn.argtypes = [wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p,
                           ctypes.c_void_p, ctypes.POINTER(wintypes.ULONG)]
            fn.restype = wintypes.ULONG
            _win_gaa = fn
        size = wintypes.ULONG(15000)
        buf = ctypes.create_string_buffer(size.value)
        rc = _win_gaa(0, _WIN_GAA_FLAGS, None, buf, ctypes.byref(size))   # 0 = AF_UNSPEC
        if rc == 111:                                                     # ERROR_BUFFER_OVERFLOW
            buf = ctypes.create_string_buffer(size.value)
            rc = _win_gaa(0, _WIN_GAA_FLAGS, None, buf, ctypes.byref(size))
        if rc != 0:
            return None
        names = set()
        addr = ctypes.addressof(buf)
        while addr:
            ptr = int.from_bytes(ctypes.string_at(addr + 72, 8), 'little')   # FriendlyName
            if ptr:
                names.add(ctypes.wstring_at(ptr))
            addr = int.from_bytes(ctypes.string_at(addr + 8, 8), 'little')   # Next
        return names
    except Exception:
        return None

def _win_net_names_get():
    """适配器名集合，带 TTL。集合变化很慢，刷新一次只要几毫秒，同步做即可。"""
    global _win_net_names, _win_net_names_clock
    now = time.time()
    if _win_net_names is None or now - _win_net_names_clock >= NET_NAMES_REFRESH_INTERVAL:
        names = _win_gaa_names()
        if names:
            _win_net_names = names
            _win_net_names_clock = now
    return _win_net_names

def _win_if_table2():
    """GetIfTable2 → [(alias, InOctets, OutOctets)]；只取需要的三个字段随即释放表。
    失败返回 None。"""
    try:
        fn = getattr(_win_if_table2, 'fn', None)
        if fn is None:
            iphlp = ctypes.WinDLL('iphlpapi')
            fn = iphlp.GetIfTable2
            fn.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
            fn.restype = wintypes.ULONG
            free = iphlp.FreeMibTable
            free.argtypes = [ctypes.c_void_p]
            _win_if_table2.fn = fn
            _win_if_table2.free = free
        ptr = ctypes.c_void_p()
        if fn(ctypes.byref(ptr)) != 0 or not ptr.value:
            return None
        try:
            n = int.from_bytes(ctypes.string_at(ptr.value, 4), 'little')     # NumEntries
            if not 0 < n < 4096:
                return None
            raw = ctypes.string_at(ptr.value, 8 + n * _WIN_IF_ROW_SIZE)
        finally:
            _win_if_table2.free(ptr)
        rows = []
        for i in range(n):
            b = 8 + i * _WIN_IF_ROW_SIZE
            alias = _win_wstr_at(raw, b + _WIN_IF_ALIAS_OFF)
            if not alias:
                continue
            rows.append((alias,
                         int.from_bytes(raw[b + _WIN_IF_IN_OFF:b + _WIN_IF_IN_OFF + 8], 'little'),
                         int.from_bytes(raw[b + _WIN_IF_OUT_OFF:b + _WIN_IF_OUT_OFF + 8], 'little')))
        return rows
    except Exception:
        return None

def _win_net_io_native():
    """原生网卡计数：GetIfTable2 一次调用，按适配器名集合筛选，返回
    {name: (bytes_sent, bytes_recv)}——与 psutil 的 pernic 计数同键集、同字段序
    （可直接喂给 _sum_physical_counters）。失败返回 None。"""
    rows = _win_if_table2()
    if rows is None:
        return None
    names = _win_net_names_get()
    if not names:
        return None
    return {alias: (vout, vin) for alias, vin, vout in rows if alias in names}

def _win_net_io_selfcheck():
    """首次对账：适配器名集合必须与 psutil 完全相同，且总量被前后两次原生读数夹住
    （避免读取时差造成误判）。任一不满足即永久退回 psutil——这些结构偏移未文档化，
    宁可退回慢路径也不能报错数。通过则返回本次原生读数。"""
    n1 = _win_net_io_native()
    if n1 is None:
        return None
    ps = _psutil_net_io()
    n2 = _win_net_io_native()
    if n2 is None or set(n1) != set(ps):
        return None
    for i in (0, 1):        # 0=bytes_sent, 1=bytes_recv
        lo = sum(v[i] for v in n1.values())
        hi = sum(v[i] for v in n2.values())
        cur = sum(v[i] for v in ps.values())
        if not lo <= cur <= hi + (1 << 20):
            return None
    return n2

_win_tcp_table = None

def _win_tcp_conn_count():
    """Windows TCP 连接数：直接读 GetExtendedTcpTable 返回表的 dwNumEntries，只为计数，
    不构造每条连接对象——下载类机器上万连接时 psutil.net_connections 可达 70ms+。
    实测（含 IPv4+IPv6）与 psutil.net_connections('tcp') 计数一致。失败返回 None。"""
    global _win_tcp_table
    try:
        if _win_tcp_table is None:
            fn = ctypes.WinDLL('iphlpapi').GetExtendedTcpTable
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD), wintypes.BOOL,
                           wintypes.DWORD, ctypes.c_int, wintypes.DWORD]
            fn.restype = wintypes.DWORD
            _win_tcp_table = fn
        total = 0
        for af in (2, 23):                        # AF_INET / AF_INET6
            for _ in range(3):                    # 表在两次调用间可能增长，失败重取
                size = wintypes.DWORD(0)
                _win_tcp_table(None, ctypes.byref(size), False, af, 5, 0)   # 5 = TCP_TABLE_OWNER_PID_ALL
                if size.value == 0:
                    break
                buf = ctypes.create_string_buffer(size.value)
                if _win_tcp_table(buf, ctypes.byref(size), False, af, 5, 0) == 0:
                    total += int.from_bytes(buf.raw[:4], 'little')          # dwNumEntries
                    break
        return total
    except Exception:
        return None

def tupd():
    '''
    tcp, udp, process, thread count: for view ddcc attack , then send warning
    :return:
    '''
    try:
        if sys.platform.startswith("linux") is True:
            t = int(os.popen('ss -t|wc -l').read()[:-1])-1
            u = int(os.popen('ss -u|wc -l').read()[:-1])-1
            p = int(os.popen('ps -ef|wc -l').read()[:-1])-2
            d = int(os.popen('ps -eLf|wc -l').read()[:-1])-2
        elif sys.platform.startswith("darwin") is True:
            t = int(os.popen('lsof -nP -iTCP  | wc -l').read()[:-1]) - 1
            u = int(os.popen('lsof -nP -iUDP  | wc -l').read()[:-1]) - 1
            p = len(psutil.pids())
            d = 0
            for k in psutil.pids():
                try:
                    d += psutil.Process(k).num_threads()
                except:
                    pass

        elif sys.platform.startswith("win") is True:
            # Windows：走原生 API（与任务管理器同源），避免 netstat 管道 + 逐进程遍历（约 7s）
            t = _win_tcp_conn_count()                         # GetExtendedTcpTable，只读计数
            if t is None:
                try:
                    t = len(psutil.net_connections(kind='tcp'))
                except Exception:
                    t = 0
            try:
                # UDP 表原生口径与 psutil 不一致（实测 37 vs 47），且开销极小，保留 psutil
                u = len(psutil.net_connections(kind='udp'))
            except Exception:
                u = 0
            p, d, _ = _win_sys_counts()                       # NtQuerySystemInformation
        else:
            t,u,p,d = 0,0,0,0
        return t,u,p,d
    except:
        return 0,0,0,0

_tupd_snapshot = (0, 0, 0, 0)

def _tupd_thread():
    """独立线程：每 TUP_INTERVAL 秒采样一次 tcp/udp/process/thread 计数，
    主循环只读 _tupd_snapshot 快照，避免每轮 fork 子进程（Linux）或遍历连接表（Windows）。
    Windows 上顺带用同一次原生快照里的就绪队列长度推进 load 的 EWMA——省掉原先
    load-avg 线程单独走 PDH 的 System provider（单次 4~9ms）。"""
    _name_current_thread('tupd')
    global _tupd_snapshot
    while True:
        _tupd_snapshot = tupd()
        if sys.platform.startswith("win") is True:
            _win_load_step(_win_ready_last)
        time.sleep(TUP_INTERVAL)

def get_network(ip_version):
    if(ip_version == 4):
        HOST = "ipv4.ip.sb"
    elif(ip_version == 6):
        HOST = "ipv6.ip.sb"
    try:
        socket.create_connection((HOST, PROBEPORT), 2).close()
        return True
    except:
        return False

lostRate = {
    '10010': 0.0,
    '189': 0.0,
    '10086': 0.0
}
pingTime = {
    '10010': 0,
    '189': 0,
    '10086': 0
}
netSpeed = {
    'netrx': 0.0,
    'nettx': 0.0,
    'clock': 0.0,
    'diff': 0.0,
    'avgrx': 0,
    'avgtx': 0
}
diskIO = {
    'read': 0,
    'write': 0
}
monitorServer = {}

def update_net_speed(avgrx, avgtx, now_clock=None):
    if now_clock is None:
        now_clock = time.monotonic()
    previous_clock = netSpeed.get("clock", 0.0)
    previous_rx = netSpeed.get("avgrx", 0)
    previous_tx = netSpeed.get("avgtx", 0)
    diff = now_clock - previous_clock
    initialized = previous_clock > 0 and diff > 0
    netSpeed["diff"] = diff if initialized else 0.0
    netSpeed["clock"] = now_clock
    netSpeed["netrx"] = int((avgrx - previous_rx) / diff) if initialized and avgrx >= previous_rx else 0
    netSpeed["nettx"] = int((avgtx - previous_tx) / diff) if initialized and avgtx >= previous_tx else 0
    netSpeed["avgrx"] = avgrx
    netSpeed["avgtx"] = avgtx
    return netSpeed["netrx"], netSpeed["nettx"]

def _should_resolve(host, last_resolve, now, interval):
    """是否需要重新解析探针域名。抽成纯函数便于单测（_ping_thread 是无限循环）。
    host 为纯 IPv6 字面量时无需解析；last_resolve 为 None 表示立即解析（首次或建连失败后）。"""
    if host.count(':') >= 1:  # 纯 IPv6 字面量，无需解析
        return False
    return last_resolve is None or now - last_resolve >= interval

def _ping_thread(host, mark, port):
    _name_current_thread('ping-' + str(mark))
    lostPacket = 0
    packet_queue = Queue(maxsize=PING_PACKET_HISTORY_LEN)

    # DNS 解析按 DNS_REFRESH_INTERVAL 节流（探针域名 TTL 约 180s）。
    # IP 必须提到循环外：否则跳过的轮次 IP=域名，create_connection 仍会解析一次，节流失效。
    # 建连频率仍由 INTERVAL 决定，丢包窗口 PING_PACKET_HISTORY_LEN*INTERVAL 不变。
    IP = host
    cached_ip = None
    last_resolve = None

    while True:
        now = time.monotonic()  # 与 update_net_speed 一致，不受系统时钟回拨影响
        if _should_resolve(host, last_resolve, now, DNS_REFRESH_INTERVAL):
            last_resolve = now
            try:
                if PROBE_PROTOCOL_PREFER == 'ipv4':
                    cached_ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
                else:
                    cached_ip = socket.getaddrinfo(host, None, socket.AF_INET6)[0][4][0]
            except Exception:
                cached_ip = cached_ip or host  # 沿用上次成功值；首次失败交给 create_connection 兜底
            IP = cached_ip

        if packet_queue.full():
            if packet_queue.get() == 0:
                lostPacket -= 1
        try:
            b = timeit.default_timer()
            socket.create_connection((IP, port), timeout=1).close()
            pingTime[mark] = int((timeit.default_timer() - b) * 1000)
            packet_queue.put(1)
        except socket.error as error:
            if error.errno == errno.ECONNREFUSED:
                pingTime[mark] = int((timeit.default_timer() - b) * 1000)
                packet_queue.put(1)
            #elif error.errno == errno.ETIMEDOUT:
            else:
                lostPacket += 1
                packet_queue.put(0)
                last_resolve = None  # 建连失败→下轮强制重解析，避免钉死已失效 IP

        if packet_queue.qsize() > 30:
            lostRate[mark] = float(lostPacket) / packet_queue.qsize()

        time.sleep(INTERVAL)

def _disk_io():
    """
    the code is by: https://github.com/giampaolo/psutil/blob/master/scripts/iotop.py
    good luck for opensource! modify: cpp.la
    Calculate IO usage by comparing IO statics before and
        after the interval.
    磁盘IO：因为IOPS原因，SSD和HDD、包括RAID卡，ZFS等。IO对性能的影响还需要结合自身服务器情况来判断。
    比如我这里是机械硬盘，大量做随机小文件读写，那么很低的读写也就能造成硬盘长时间的等待。
    如果这里做连续性IO，那么普通机械硬盘写入到100Mb/s，那么也能造成硬盘长时间的等待。
    磁盘读写有误差：4k，8k ，https://stackoverflow.com/questions/34413926/psutil-vs-dd-monitoring-disk-i-o
    macos/win，暂不处理。
    """
    _name_current_thread('disk-io')
    if "darwin" in sys.platform or "win" in sys.platform:
        diskIO["read"] = 0
        diskIO["write"] = 0
    else:
        while True:
            # sleep some time, only when INTERVAL==1 , io read/write per_sec.
            # when INTERVAL > 1, io read/write per_INTERVAL
            disks_before = psutil.disk_io_counters()
            time.sleep(INTERVAL)
            disks_after = psutil.disk_io_counters()
            diskIO["read"] = disks_after.read_bytes - disks_before.read_bytes
            diskIO["write"] = disks_after.write_bytes - disks_before.write_bytes

def get_realtime_data():
    '''
    real time get system data
    :return:
    '''
    t1 = threading.Thread(
        target=_ping_thread,
        kwargs={
            'host': CU,
            'mark': '10010',
            'port': PROBEPORT
        }
    )
    t2 = threading.Thread(
        target=_ping_thread,
        kwargs={
            'host': CT,
            'mark': '189',
            'port': PROBEPORT
        }
    )
    t3 = threading.Thread(
        target=_ping_thread,
        kwargs={
            'host': CM,
            'mark': '10086',
            'port': PROBEPORT
        }
    )
    t4 = threading.Thread(
        target=_net_monitor,
    )
    t5 = threading.Thread(
        target=_disk_io,
    )
    t6 = threading.Thread(
        target=_net_probe_thread,
    )
    t7 = threading.Thread(
        target=_tupd_thread,
    )
    threads = [t1, t2, t3, t4, t5, t6, t7]
    for ti in threads:
        ti.daemon = True
        ti.start()

def _monitor_thread(name, host, interval, type):
    # 参考 _ping_thread 风格：按协议族偏好解析 IP，测 TCP 建连耗时
    _name_current_thread('monitor-' + str(name))
    IP = None
    cached_ip = None
    last_resolve = None
    while True:
        if name not in monitorServer:
            break
        try:
            # 1) 解析目标 host 与端口
            if type == 'http':
                addr = str(host).replace('http://','')
                addr = addr.split('/',1)[0]
                port = 80
                if ':' in addr and not addr.startswith('['):
                    a, p = addr.rsplit(':',1)
                    if p.isdigit():
                        addr, port = a, int(p)
            elif type == 'https':
                addr = str(host).replace('https://','')
                addr = addr.split('/',1)[0]
                port = 443
                if ':' in addr and not addr.startswith('['):
                    a, p = addr.rsplit(':',1)
                    if p.isdigit():
                        addr, port = a, int(p)
            elif type == 'tcp':
                addr = str(host)
                if addr.startswith('[') and ']' in addr:
                    # [v6]:port
                    a = addr[1:addr.index(']')]
                    rest = addr[addr.index(']')+1:]
                    if rest.startswith(':') and rest[1:].isdigit():
                        addr, port = a, int(rest[1:])
                    else:
                        raise Exception('bad tcp target')
                else:
                    a, p = addr.rsplit(':',1)
                    addr, port = a, int(p)
            else:
                time.sleep(interval)
                continue

            # 2) 解析 IP（按偏好族），与 _ping_thread 保持一致的判定与 DNS 节流
            if addr.count(':') < 1:  # 非纯 IPv6，可能是 IPv4 或域名
                now = time.monotonic()
                if _should_resolve(addr, last_resolve, now, DNS_REFRESH_INTERVAL):
                    last_resolve = now
                    try:
                        if PROBE_PROTOCOL_PREFER == 'ipv4':
                            cached_ip = socket.getaddrinfo(addr, None, socket.AF_INET)[0][4][0]
                        else:
                            cached_ip = socket.getaddrinfo(addr, None, socket.AF_INET6)[0][4][0]
                    except Exception:
                        pass
                IP = cached_ip or addr
            else:
                IP = addr

            # 3) 测 TCP 建连耗时（timeout=1s）；ECONNREFUSED 也记为耗时
            try:
                b = timeit.default_timer()
                socket.create_connection((IP, port), timeout=1).close()
                monitorServer[name]['latency'] = int((timeit.default_timer() - b) * 1000)
            except socket.error as error:
                if getattr(error, 'errno', None) == errno.ECONNREFUSED:
                    monitorServer[name]['latency'] = int((timeit.default_timer() - b) * 1000)
                else:
                    monitorServer[name]['latency'] = 0
                    last_resolve = None  # 建连失败→下轮强制重解析，避免钉死已失效 IP
        except Exception:
            monitorServer[name]['latency'] = 0
        time.sleep(interval)


def byte_str(object):
    '''
    bytes to str, str to bytes
    :param object:
    :return:
    '''
    if isinstance(object, str):
        return object.encode(encoding="utf-8")
    elif isinstance(object, bytes):
        return bytes.decode(object)
    else:
        print(type(object))

if __name__ == '__main__':
    cli_args = parse_cli_args(sys.argv[1:])
    SERVER = cli_args.get('SERVER', SERVER)
    PORT = int(cli_args.get('PORT', PORT))
    USER = cli_args.get('USER', USER)
    PASSWORD = cli_args.get('PASSWORD', PASSWORD)
    INTERVAL = int(cli_args.get('INTERVAL', INTERVAL))
    socket.setdefaulttimeout(30)
    get_realtime_data()
    while 1:
        try:
            print("Connecting...")
            s = socket.create_connection((SERVER, PORT))
            data = byte_str(s.recv(1024))
            if data.find("Authentication required") > -1:
                s.send(byte_str(USER + ':' + PASSWORD + '\n'))
                data = byte_str(s.recv(1024))
                if data.find("Authentication successful") < 0:
                    print(data)
                    raise socket.error
            else:
                print(data)
                raise socket.error

            print(data)
            if data.find("You are connecting via") < 0:
                data = byte_str(s.recv(1024))
                print(data)
                for i in data.split('\n'):
                    if "monitor" in i and "type" in i and "{" in i and "}" in i:
                        jdata = json.loads(i[i.find("{"):i.find("}")+1])
                        monitorServer[jdata.get("name")] = {
                            "type": jdata.get("type"),
                            "host": jdata.get("host"),
                            "latency": 0
                        }
                        t = threading.Thread(
                            target=_monitor_thread,
                            kwargs={
                                'name': jdata.get("name"),
                                'host': jdata.get("host"),
                                'interval': jdata.get("interval"),
                                'type': jdata.get("type")
                            }
                        )
                        t.daemon = True
                        t.start()

            check_ip = 0
            if data.find("IPv4") > -1:
                check_ip = 6
            elif data.find("IPv6") > -1:
                check_ip = 4
            else:
                print(data)
                raise socket.error

            _online_target = check_ip
            CPUCores = get_cpu_cores()
            CPUModel = get_cpu_model()
            while 1:
                CPU = get_cpu()
                NET_IN, NET_OUT = liuliang()
                Uptime = get_uptime()
                if 'linux' in sys.platform or 'darwin' in sys.platform:
                    Load_1, Load_5, Load_15 = os.getloadavg()
                elif sys.platform.startswith('win'):
                    with _win_load_lock:
                        Load_1, Load_5, Load_15 = _win_load_averages
                else:
                    Load_1, Load_5, Load_15 = 0.0, 0.0, 0.0
                MemoryTotal, MemoryUsed = get_memory()
                SwapTotal, SwapUsed = get_swap()
                HDDTotal, HDDUsed = get_hdd()
                array = {}
                # 在线探测已由独立线程 _net_probe_thread 负责，主循环只读快照（DNS 卡死不阻塞上报）
                array['online' + str(check_ip)] = _online_status.get(check_ip, False)

                array['uptime'] = Uptime
                array['load_1'] = Load_1
                array['load_5'] = Load_5
                array['load_15'] = Load_15
                array['memory_total'] = MemoryTotal
                array['memory_used'] = MemoryUsed
                array['swap_total'] = SwapTotal
                array['swap_used'] = SwapUsed
                array['hdd_total'] = HDDTotal
                array['hdd_used'] = HDDUsed
                array['cpu'] = CPU
                array['cpu_cores'] = CPUCores
                array['cpu_model'] = CPUModel
                array['network_rx'] = netSpeed.get("netrx")
                array['network_tx'] = netSpeed.get("nettx")
                array['network_in'] = NET_IN
                array['network_out'] = NET_OUT
                array['ping_10010'] = lostRate.get('10010') * 100
                array['ping_189'] = lostRate.get('189') * 100
                array['ping_10086'] = lostRate.get('10086') * 100
                array['time_10010'] = pingTime.get('10010')
                array['time_189'] = pingTime.get('189')
                array['time_10086'] = pingTime.get('10086')
                array['tcp'], array['udp'], array['process'], array['thread'] = _tupd_snapshot
                array['io_read'] = diskIO.get("read")
                array['io_write'] = diskIO.get("write")
                array['os'] = get_os_name()
                items = []
                for _n, st in monitorServer.items():
                    key = str(_n)
                    try:
                        ms = int(st.get('latency') or 0)
                    except Exception:
                        ms = 0
                    items.append((key, max(0, ms)))
                # 稳定顺序：按 key 排序
                items.sort(key=lambda x: x[0])
                array['custom'] = ';'.join(f"{k}={v}" for k,v in items)
                s.send(byte_str("update " + json.dumps(array) + "\n"))
        except KeyboardInterrupt:
            raise
        except socket.error:
            monitorServer.clear()
            print("Disconnected...")
            if 's' in locals().keys():
                del s
            time.sleep(3)
        except Exception as e:
            monitorServer.clear()
            print("Caught Exception:", e)
            if 's' in locals().keys():
                del s
            time.sleep(3)
