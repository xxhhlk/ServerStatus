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

def get_uptime():
    return int(time.time() - psutil.boot_time())

def _win_mem_no_cache():
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

def get_memory():
    if sys.platform.startswith("win"):
        r = _win_mem_no_cache()
        if r:
            return r
    Mem = psutil.virtual_memory()
    return int(Mem.total / 1024.0), int(Mem.used / 1024.0)

def get_swap():
    Mem = psutil.swap_memory()
    return int(Mem.total/1024.0), int(Mem.used/1024.0)

def get_hdd():
    if "darwin" in sys.platform:
        return int(psutil.disk_usage("/").total/1024.0/1024.0), int((psutil.disk_usage("/").total-psutil.disk_usage("/").free)/1024.0/1024.0)
    elif sys.platform.startswith("win"):
        # Windows：只统计系统盘（SystemDrive，通常 C:），避免多盘/外置盘混入
        sysdrive = os.environ.get("SystemDrive", "C:") + os.sep
        usage = psutil.disk_usage(sysdrive)
        return int(usage.total/1024.0/1024.0), int(usage.used/1024.0/1024.0)
    else:
        valid_fs = ["ext4", "ext3", "ext2", "reiserfs", "jfs", "btrfs", "fuseblk", "zfs", "simfs", "ntfs", "fat32",
                    "exfat", "xfs"]
        disks = dict()
        size = 0
        used = 0
        for disk in psutil.disk_partitions():
            if not disk.device in disks and disk.fstype.lower() in valid_fs:
                disks[disk.device] = disk.mountpoint
        for disk in disks.values():
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
_WIN_NAME_HINTS = ('Loopback', 'Wi-Fi Direct', 'WAN Miniport', 'Bluetooth',
                   'Apple Mobile', 'Kernel Debug', 'TAP-', 'OpenVPN',
                   'Tailscale', 'WireGuard', 'VMware', 'VirtualBox',
                   'Host-Only', 'vEthernet', 'Default Switch')

def _win_is_physical(pnp_id):
    """PCI 或 USB 开头 → 物理网卡"""
    return bool(pnp_id) and pnp_id.startswith(('PCI\\', 'USB\\'))

def _win_build_map():
    """NetConnectionID → 是否物理。10 分钟缓存一次，避免每轮调 PowerShell"""
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

def is_virtual_nic(name):
    """排除虚拟网卡和回环接口"""
    if sys.platform.startswith('win'):
        global _win_physical, _win_cache_clock
        now = time.time()
        if _win_physical is None or now - _win_cache_clock > 600:
            try:
                _win_physical = _win_build_map()
                _win_cache_clock = now
            except Exception:
                _win_physical = _win_physical or {}   # 失败保留旧值，宁可多统计不崩
        if name in _win_physical:
            return not _win_physical[name]
        if any(h in name for h in _WIN_NAME_HINTS):
            return True
        try:
            return psutil.net_if_stats()[name].speed == 0
        except Exception:
            return False
    if name == 'lo':
        return True
    if not os.access('/sys/class/net', os.R_OK):
        return False   # /sys 不可读（Android 非 root）→ 无法判断，宁可计入
    return not os.path.exists('/sys/class/net/%s/device' % name)

_net_in = 0
_net_out = 0
_net_lock = threading.Lock()

def _net_monitor():
    """独立线程：唯一调用 psutil.net_io_counters 的地方，读累计值存全局并算网速"""
    global _net_in, _net_out
    prev_in = 0
    prev_out = 0
    prev_clock = 0
    while True:
        try:
            total_in = 0
            total_out = 0
            net = psutil.net_io_counters(pernic=True)
            for k, v in net.items():
                if is_virtual_nic(k):
                    continue
                total_in += v[1]
                total_out += v[0]
            with _net_lock:
                _net_in = total_in
                _net_out = total_out
            now_clock = time.time()
            if prev_clock > 0:
                diff = now_clock - prev_clock
                if diff > 0:
                    netSpeed["netrx"] = int((total_in - prev_in) / diff)
                    netSpeed["nettx"] = int((total_out - prev_out) / diff)
            prev_in = total_in
            prev_out = total_out
            prev_clock = now_clock
        except Exception:
            # 保留最后有效值，短暂等待后自动重试，避免线程永久退出
            pass
        time.sleep(INTERVAL)

_online_status = {4: False, 6: False}
_online_target = 4

def _net_probe_thread():
    """独立线程：每 10s 探测一次 online4/online6（get_network），避免 DNS 卡死阻塞主循环上报。
    主循环只读 _online_status 快照；_online_target 由主循环在连接后设置。"""
    global _online_status
    while True:
        target = _online_target
        try:
            _online_status[target] = get_network(target)
        except Exception:
            _online_status[target] = False
        time.sleep(10)

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

def _win_proc_thread_count():
    """NtQuerySystemInformation(SystemProcessInformation)：一次系统调用拿全部进程/线程数。
    与任务管理器同源，替代逐进程 psutil.Process().num_threads()（Windows 上极慢）。"""
    nqsi = _win_nqsi()
    buf_size = 1 << 20          # 1MB 起
    while True:
        buf = ctypes.create_string_buffer(buf_size)
        ret_len = wintypes.ULONG(0)
        status = nqsi(5, buf, buf_size, ctypes.byref(ret_len))   # 5 = SystemProcessInformation
        if status == 0:                                          # STATUS_SUCCESS
            break
        if status == 0xC0000004:                                 # STATUS_INFO_LENGTH_MISMATCH → 扩大重试
            buf_size = ret_len.value + 0x10000
            continue
        return 0, 0
    b = buf.raw
    procs = threads = off = 0
    while off < len(b):
        nxt = int.from_bytes(b[off:off+4], 'little')
        threads += int.from_bytes(b[off+4:off+8], 'little')      # NumberOfThreads
        procs += 1
        if nxt == 0:
            break
        off += nxt
    return procs, threads

# --- Windows 负载（load average 近似） ---
# 就绪队列长度用文档化 PDH 计数器 \System\Processor Queue Length（所有处理器就绪队列线程数之和），
# 运行线程数用每核 CPU 利用率连续求和（单核利用率 60% 记 0.6，用 SystemProcessorPerformanceInformation 采样）。
# 1/5/15 分钟用内核同款指数衰减 EWMA：load = prev*exp(-dt/tau) + instant*(1-exp(-dt/tau))。
_win_load_averages = [0.0, 0.0, 0.0]          # [1min, 5min, 15min]，主循环只读快照
_win_load_lock = threading.Lock()

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

_pdh = None
_pdh_query = None
_pdh_counter = None

class _PDH_FMT_COUNTERVALUE(ctypes.Structure):
    _fields_ = [('CStatus', wintypes.LONG), ('value', ctypes.c_longlong)]

def _win_pdh_init():
    global _pdh, _pdh_query, _pdh_counter
    if _pdh is not None:
        return True
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
                                                    ctypes.POINTER(_PDH_FMT_COUNTERVALUE)]
        pdh.PdhGetFormattedCounterValue.restype = wintypes.LONG
        query = wintypes.HANDLE()
        counter = wintypes.HANDLE()
        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(query)) != 0:
            return False
        if pdh.PdhAddEnglishCounterW(query, "\\System\\Processor Queue Length", 0, ctypes.byref(counter)) != 0:
            return False
        pdh.PdhCollectQueryData(query)   # 首次采集仅初始化，数据下一轮就绪
        _pdh, _pdh_query, _pdh_counter = pdh, query, counter
        return True
    except Exception:
        return False

def _win_pdh_queue_length():
    """当前就绪队列线程数；PDH 数据未就绪/失败返回 None"""
    if _pdh is None:
        return None
    pdh, query, counter = _pdh, _pdh_query, _pdh_counter
    if pdh.PdhCollectQueryData(query) != 0:
        return None
    value = _PDH_FMT_COUNTERVALUE()
    ctype = wintypes.DWORD(0)
    # 0x400 = PDH_FMT_LARGE：union 按 8 字节 LARGE_INTEGER 读，与结构布局一致
    if pdh.PdhGetFormattedCounterValue(counter, 0x400, ctypes.byref(ctype), ctypes.byref(value)) != 0:
        return None
    if value.CStatus not in (0, 1):   # PDH_CSTATUS_VALID_DATA / NEW_DATA，其余为无效/待更新
        return None
    return max(0, value.value)

def _win_load_thread():
    """独立线程：每 5s 采一次瞬时负载，做 1/5/15 分钟 EWMA，主循环只读快照。
    瞬时负载 = 就绪队列线程数 + Σ(每核 CPU 利用率)（连续值，与 Linux 语义一致：
    单核跑满记 1、利用率 60% 记 0.6，避免离散阈值在中等负载区间低估）。
    启动后 2 分钟为 warm-up：直接报瞬时值，避免 PDH 数据未就绪/无基线时
    把起点钉在 0，导致监控读数从 0 缓慢爬升。"""
    tau = (60.0, 300.0, 900.0)
    prev_idle = None
    prev_clock = 0.0
    warm_until = time.time() + 120
    _win_pdh_init()   # PDH 不可用时就绪队列恒 0，负载退化为 Σ利用率
    while True:
        try:
            qlen = _win_pdh_queue_length()
            perfs = _win_perf_snapshot()
            if perfs is not None or qlen is not None:
                running = 0.0
                if perfs is not None and prev_idle is not None:
                    for cur, prev in zip(perfs, prev_idle):
                        # 注意：KernelTime 包含 IdleTime，必须先减掉再算内核忙碌时间
                        idle = cur.IdleTime - prev.IdleTime
                        busy = (cur.KernelTime - prev.KernelTime) - idle + (cur.UserTime - prev.UserTime)
                        total = busy + idle
                        if total > 0:
                            running += busy / total
                if perfs is not None:
                    prev_idle = perfs
                instant = (qlen or 0) + running
                now = time.time()
                if now < warm_until:
                    with _win_load_lock:
                        _win_load_averages[:] = [float(instant)] * 3
                elif prev_clock > 0:
                    dt = now - prev_clock
                    avg = list(_win_load_averages)
                    for i, t in enumerate(tau):
                        f = math.exp(-dt / t)
                        avg[i] = avg[i] * f + instant * (1.0 - f)
                    with _win_load_lock:
                        _win_load_averages[:] = avg
                else:
                    with _win_load_lock:
                        _win_load_averages[:] = [float(instant)] * 3
                prev_clock = now
        except Exception:
            pass
        time.sleep(5)

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
            t = 0
            u = 0
            try:
                t = len(psutil.net_connections(kind='tcp'))   # GetExtendedTcpTable
                u = len(psutil.net_connections(kind='udp'))   # GetExtendedUdpTable
            except Exception:
                t = 0
                u = 0
            p, d = _win_proc_thread_count()                   # NtQuerySystemInformation
        else:
            t,u,p,d = 0,0,0,0
        return t,u,p,d
    except:
        return 0,0,0,0

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

def _ping_thread(host, mark, port):
    lostPacket = 0
    packet_queue = Queue(maxsize=PING_PACKET_HISTORY_LEN)

    while True:
        # flush dns, every time.
        IP = host
        if host.count(':') < 1:  # if not plain ipv6 address, means ipv4 address or hostname
            try:
                if PROBE_PROTOCOL_PREFER == 'ipv4':
                    IP = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
                else:
                    IP = socket.getaddrinfo(host, None, socket.AF_INET6)[0][4][0]
            except Exception:
                pass

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

        if packet_queue.qsize() > 30:
            lostRate[mark] = float(lostPacket) / packet_queue.qsize()

        time.sleep(INTERVAL)

def _disk_io():
    """
    the code is by: https://github.com/giampaolo/psutil/blob/master/scripts/iotop.py
    good luck for opensource! modify: cpp.la
    Calculate IO usage by comparing IO statics before and
        after the interval.
        Return a tuple including all currently running processes
        sorted by IO activity and total disks I/O activity.
    磁盘IO：因为IOPS原因，SSD和HDD、包括RAID卡，ZFS等。IO对性能的影响还需要结合自身服务器情况来判断。
    比如我这里是机械硬盘，大量做随机小文件读写，那么很低的读写也就能造成硬盘长时间的等待。
    如果这里做连续性IO，那么普通机械硬盘写入到100Mb/s，那么也能造成硬盘长时间的等待。
    磁盘读写有误差：4k，8k ，https://stackoverflow.com/questions/34413926/psutil-vs-dd-monitoring-disk-i-o
    macos/win，暂不处理。
    """
    if "darwin" in sys.platform or "win" in sys.platform:
        diskIO["read"] = 0
        diskIO["write"] = 0
    else:
        while True:
            # first get a list of all processes and disk io counters
            procs = [p for p in psutil.process_iter()]
            for p in procs[:]:
                try:
                    p._before = p.io_counters()
                except psutil.Error:
                    procs.remove(p)
                    continue
            disks_before = psutil.disk_io_counters()

            # sleep some time, only when INTERVAL==1 , io read/write per_sec.
            # when INTERVAL > 1, io read/write per_INTERVAL
            time.sleep(INTERVAL)

            # then retrieve the same info again
            for p in procs[:]:
                with p.oneshot():
                    try:
                        p._after = p.io_counters()
                        p._cmdline = ' '.join(p.cmdline())
                        if not p._cmdline:
                            p._cmdline = p.name()
                        p._username = p.username()
                    except (psutil.NoSuchProcess, psutil.ZombieProcess):
                        procs.remove(p)
            disks_after = psutil.disk_io_counters()

            # finally calculate results by comparing data before and
            # after the interval
            for p in procs:
                p._read_per_sec = p._after.read_bytes - p._before.read_bytes
                p._write_per_sec = p._after.write_bytes - p._before.write_bytes
                p._total = p._read_per_sec + p._write_per_sec

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
    threads = [t1, t2, t3, t4, t5, t6]
    if sys.platform.startswith('win'):
        threads.append(threading.Thread(target=_win_load_thread))
    for ti in threads:
        ti.daemon = True
        ti.start()

def _monitor_thread(name, host, interval, type):
    # 参考 _ping_thread 风格：每轮解析一次目标，按协议族偏好解析 IP，测 TCP 建连耗时
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

            # 2) 解析 IP（按偏好族），与 _ping_thread 保持一致的判定
            IP = addr
            if addr.count(':') < 1:  # 非纯 IPv6，可能是 IPv4 或域名
                try:
                    if PROBE_PROTOCOL_PREFER == 'ipv4':
                        IP = socket.getaddrinfo(addr, None, socket.AF_INET)[0][4][0]
                    else:
                        IP = socket.getaddrinfo(addr, None, socket.AF_INET6)[0][4][0]
                except Exception:
                    pass

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
                array['tcp'], array['udp'], array['process'], array['thread'] = tupd()
                array['io_read'] = diskIO.get("read")
                array['io_write'] = diskIO.get("write")
                # report OS (normalized)
                try:
                    sysname = platform.system().lower()
                    if sysname.startswith('windows'):
                        os_name = 'windows'
                    elif sysname.startswith('darwin') or 'mac' in sysname:
                        os_name = 'darwin'
                    elif 'bsd' in sysname:
                        os_name = 'bsd'
                    elif sysname.startswith('linux'):
                        # try distro from os-release
                        try:
                            with open('/etc/os-release') as f:
                                for line in f:
                                    if line.startswith('ID='):
                                        val = line.strip().split('=',1)[1].strip().strip('"')
                                        if val: os_name = val
                                        break
                        except Exception:
                            os_name = 'linux'
                    else:
                        os_name = sysname or 'unknown'
                except Exception:
                    os_name = 'unknown'
                array['os'] = os_name
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
