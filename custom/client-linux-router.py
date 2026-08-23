#!/usr/bin/env python3
# coding: utf-8
# Update by : https://github.com/cppla/ServerStatus, Update date: 20250902
# 版本：1.1.0, 支持Python版本：3.6+
# 支持操作系统： Linux, OSX, FreeBSD, OpenBSD and NetBSD, both 32-bit and 64-bit architectures
# 说明: 默认情况下修改server和user就可以了。丢包率监测方向可以自定义，例如：CU = "www.facebook.com"。
# 华硕路由器：自动读取 nvram productid（如 RT-BE88U）并对照内置型号-SoC 表显示芯片型号（如 BCM4916）。
# ============ 路由器定制（基于官方 1.1.0 重建） ============
# - 流量/速率只统计 wan0 接口，避免内网 LAN 流量计入
# - 流量带持久化状态文件，光猫/路由器重启后计数器不归零（封口累计）
# - 在线探测 4.ipchaxun.net / 6.ipchaxun.net
# - 丢包探针 DNS 每 150 轮解析一次，失败回退缓存 IP（省 DNS 查询、抗抖动）

SERVER = "monitor-data.xxhhlk.com"
USER = "xxhhlk"


# PASSWORD 不写入源码：部署时用环境变量 PASSWORD=xxx 传入，
# 或在此行填入（需与服务端 config.json 的 PASSWORD 一致）。
PASSWORD = ""
PORT = 37014
CU = "gd-guangzhou-cu-v4.ip.zstaticcdn.com"
CT = "gd-shenzhen-ct-v4.ip.zstaticcdn.com"
CM = "gd-guangzhou-cm-v4.ip.zstaticcdn.com"
PROBEPORT = 443
PROBE_PROTOCOL_PREFER = "ipv6"  # ipv4, ipv6
PING_PACKET_HISTORY_LEN = 200
INTERVAL = 1

import socket
import time
import timeit
import re
import os
import sys
import json
import errno
import subprocess
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
SERVER = _env_str("SERVER", SERVER) if SERVER == "" else SERVER
USER = _env_str("USER", USER) if USER == "" else USER
PASSWORD = _env_str("PASSWORD", PASSWORD)
PORT = _env_int("PORT", PORT)
INTERVAL = _env_int("INTERVAL", INTERVAL)
PROBEPORT = _env_int("PROBEPORT", PROBEPORT)
PROBE_PROTOCOL_PREFER = _env_str("PROBE_PROTOCOL_PREFER", PROBE_PROTOCOL_PREFER)
PING_PACKET_HISTORY_LEN = _env_int("PING_PACKET_HISTORY_LEN", PING_PACKET_HISTORY_LEN)
CU = _env_str("CU", CU)
CT = _env_str("CT", CT)
CM = _env_str("CM", CM)

def parse_cli_args(arguments):
    overrides = {}
    for argument in arguments:
        key, separator, value = argument.partition('=')
        if separator and key in {'SERVER', 'PORT', 'USER', 'PASSWORD', 'INTERVAL'}:
            overrides[key] = value
    return overrides

def get_uptime():
    with open('/proc/uptime', 'r') as f:
        uptime = f.readline().split('.', 2)
        return int(uptime[0])

def get_memory():
    re_parser = re.compile(r'^(?P<key>\S*):\s*(?P<value>\d*)\s*kB')
    result = dict()
    for line in open('/proc/meminfo'):
        match = re_parser.match(line)
        if not match:
            continue
        key, value = match.groups(['key', 'value'])
        result[key] = int(value)
    MemTotal = float(result['MemTotal'])
    MemUsed = MemTotal-float(result['MemFree'])-float(result['Buffers'])-float(result['Cached'])-float(result['SReclaimable'])
    SwapTotal = float(result['SwapTotal'])
    SwapFree = float(result['SwapFree'])
    return int(MemTotal), int(MemUsed), int(SwapTotal), int(SwapFree)

def get_hdd():
    valid_fs = {
        "ext4", "ext3", "ext2", "reiserfs", "jfs", "btrfs", "fuseblk",
        "zfs", "simfs", "ntfs", "fat32", "exfat", "xfs"
    }
    disks = {}
    size = 0
    used = 0
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 3:
                    continue
                device = parts[0]
                mountpoint = parts[1]
                fstype = parts[2].lower()
                if fstype not in valid_fs or device in disks:
                    continue
                disks[device] = mountpoint
        for mountpoint in disks.values():
            st = os.statvfs(mountpoint)
            total_bytes = st.f_blocks * st.f_frsize
            used_bytes = (st.f_blocks - st.f_bavail) * st.f_frsize
            size += total_bytes
            used += used_bytes
    except Exception:
        pass
    return int(size / 1024 / 1024), int(used / 1024 / 1024)

def get_time():
    with open("/proc/stat", "r") as f:
        time_list = f.readline().split(' ')[2:6]
        for i in range(len(time_list))  :
            time_list[i] = int(time_list[i])
        return time_list

def delta_time():
    x = get_time()
    time.sleep(INTERVAL)
    y = get_time()
    for i in range(len(x)):
        y[i]-=x[i]
    return y

def get_cpu():
    t = delta_time()
    st = sum(t)
    if st == 0:
        st = 1
    result = 100-(t[len(t)-1]*100.00/st)
    return round(result, 1)

def get_cpu_cores():
    try:
        with open('/proc/stat') as f:
            cores = sum(1 for line in f if re.match(r'^cpu\d+\s', line))
        if cores > 0:
            return cores
    except Exception:
        pass
    return os.cpu_count() or 0

def normalize_cpu_model(value):
    return re.sub(r'\s+', ' ', str(value or '')).strip()[:160]

def is_generic_cpu_model(value):
    v = normalize_cpu_model(value).lower().replace('-', '').replace('_', '').replace(' ', '')
    return v in ('', 'unknown', 'x8664', 'amd64', 'i386', 'i686', 'aarch64', 'arm64') or v.startswith('armv')

def get_lscpu_info():
    result = {}
    try:
        output = subprocess.check_output(['lscpu'], stderr=subprocess.DEVNULL, timeout=2).decode(errors='ignore')
        for line in output.splitlines():
            if ':' not in line:
                continue
            key, value = line.split(':', 1)
            key = key.strip().lower()
            value = normalize_cpu_model(value)
            if value and key not in result:
                result[key] = value
    except Exception:
        pass
    return result

def get_cpuinfo_values():
    result = {}
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if ':' not in line:
                    continue
                key, value = line.split(':', 1)
                key = key.strip().lower()
                value = normalize_cpu_model(value)
                if value and key not in result:
                    result[key] = value
    except Exception:
        pass
    return result

def get_cpu_model():
    cpuinfo = get_cpuinfo_values()
    lscpu = get_lscpu_info()
    for value in (
        cpuinfo.get('model name'),
        lscpu.get('model name'),
        cpuinfo.get('hardware'),
        cpuinfo.get('processor'),
        platform.processor(),
    ):
        value = normalize_cpu_model(value)
        if value and not value.isdigit() and not is_generic_cpu_model(value):
            return value
    vendor = normalize_cpu_model(lscpu.get('vendor id') or cpuinfo.get('vendor_id'))
    if vendor:
        return vendor
    return normalize_cpu_model(lscpu.get('architecture') or platform.machine() or platform.processor())

# ============ 华硕路由器型号-SoC 映射表 ============
# 数据源: asus_routers_spec_soc_full.xlsx（131 款华硕路由器）
# 键: nvram productid 规范化（大写 + 去非字母数字），如 RT-BE88U -> RTBE88U
# 值: 芯片型号（统一风格，仅芯片名，多芯片用 + 连接）
ASUS_SOC_MAP = {
    'BD4': 'IPQ5322',
    'BD4OUTDOOR': 'IPQ5322',
    'BD5': 'IPQ5322',
    'BD5OUTDOOR': 'IPQ5322',
    'BE14000': 'MT7988DV',
    'BE30000': 'BCM4916',
    'BE3600': 'IPQ5322',
    'BE5000': 'IPQ5322',
    'BLUECAVE': 'GRX350',
    'BQ16': 'BCM4916',
    'BQ16PRO': 'BCM4916',
    'BRTAC828': 'IPQ8065',
    'BT10': 'BCM6766',
    'BT6': 'MT7988DV',
    'BT8': 'MT7988DV',
    'BT8P': 'MT7988DV',
    'CT8': 'IPQ4019',
    'DSLAC68U': 'BCM4708A0+MT7510',
    'DSLAC88U': 'BCM63138+BCM4366E',
    'DSLAX82U': 'BCM6750+BCM43684',
    'ET12': 'BCM4912',
    'ET8': 'BCM6755',
    'ET9': 'BCM6756+BCM6715',
    'GS7': 'MT7988DV',
    'GS7AIR': 'MT7987A',
    'GS7PRO': 'BCM6766',
    'GSAX3000': 'BCM6750',
    'GSAX5400': 'BCM6750',
    'GSBE12000': 'BCM6766',
    'GSBE18000': 'BCM6766',
    'GSBE7200X': 'MT7988DV',
    'GT6': 'BCM6753',
    'GTAC2900': 'BCM4906',
    'GTAC5300': 'BCM4908+BCM4366E',
    'GTAX11000': 'BCM4908',
    'GTAX11000PRO': 'BCM4912',
    'GTAX6000': 'BCM4912',
    'GTAXE11000': 'BCM4908',
    'GTAXE16000': 'BCM4912',
    'GTBE19000': 'BCM4916',
    'GTBE19000AI': 'BCM4916',
    'GTBE25000': 'BCM4916',
    'GTBE96': 'BCM4916',
    'GTBE96AI': 'BCM4916',
    'GTBE98': 'BCM4916',
    'GTBE98PRO': 'BCM4916',
    'LYRAVOICE': 'IPQ4019',
    'RTAC1200': 'MT7628AN',
    'RTAC1200G': 'BCM47189',
    'RTAC1200HP': 'MT7620A',
    'RTAC1200V2': 'MT7628DAN',
    'RTAC1900': 'BCM4708A0',
    'RTAC1900P': 'BCM4709C0',
    'RTAC3100': 'BCM47094',
    'RTAC3200': 'BCM4709A0',
    'RTAC51U': 'MT7620A',
    'RTAC52U': 'MT7620A',
    'RTAC53': 'MT7620A',
    'RTAC5300': 'BCM4709C0',
    'RTAC55UHP': 'QCA9557',
    'RTAC57UV3': 'QCN5502',
    'RTAC58UV2': 'QCN5502',
    'RTAC58UV3': 'QCN5502',
    'RTAC59U': 'QCN5502',
    'RTAC59UV2': 'QCN5502',
    'RTAC65P': 'MT7621AT',
    'RTAC66UB1': 'BCM4708C0',
    'RTAC68U': 'BCM4708A0',
    'RTAC85P': 'MT7621AT',
    'RTAC86U': 'BCM4906',
    'RTAC87U': 'BCM4709A0',
    'RTAC88U': 'BCM4709C0',
    'RTACRH12': 'QCN5502',
    'RTACRH13': 'IPQ4018+IPQ4019',
    'RTAX1800': 'MT7621AT',
    'RTAX1800HP': 'MT7621AT',
    'RTAX1800PLUS': 'BCM6755',
    'RTAX1800S': 'MT7621AT',
    'RTAX3000': 'BCM6750',
    'RTAX3000P': 'BCM6756',
    'RTAX3000S': 'MT7981B',
    'RTAX52': 'MT7981BA',
    'RTAX52PRO': 'Filogic 820',
    'RTAX53U': 'MT7621AT',
    'RTAX54': 'MT7621AT+MT7981B',
    'RTAX5400': 'BCM6750+BCM6715',
    'RTAX54HP': 'MT7621AT+MT7981B',
    'RTAX55': 'BCM6755',
    'RTAX56U': 'BCM6755',
    'RTAX56UV2': 'BCM6755',
    'RTAX57': 'BCM6756',
    'RTAX57GO': 'MT7981BA',
    'RTAX57M': 'BCM6756',
    'RTAX58U': 'BCM6750',
    'RTAX58UV2': 'BCM6755',
    'RTAX59U': 'MT7986AV',
    'RTAX68U': 'BCM4906',
    'RTAX82U': 'BCM6750',
    'RTAX82UV2': 'BCM6750+BCM6715',
    'RTAX86S': 'BCM4906',
    'RTAX86U': 'BCM4908',
    'RTAX86UPRO': 'BCM4912',
    'RTAX88U': 'BCM4908',
    'RTAX88UPRO': 'BCM4912',
    'RTAX89X': 'IPQ8074A',
    'RTAX92U': 'BCM4906',
    'RTAXE7800': 'BCM6756',
    'RTBE14000': 'MT7988DV',
    'RTBE18000': 'BCM6766',
    'RTBE3600': 'BCM6764L',
    'RTBE50': 'IPQ5312',
    'RTBE55': 'BCM6764',
    'RTBE57': 'IPQ5312',
    'RTBE58GO': 'BCM6764',
    'RTBE58U': 'BCM6764L',
    'RTBE58UV2': 'BCM6764',
    'RTBE7200': 'MT7988DV',
    'RTBE82U': 'BCM6766',
    'RTBE86U': 'BCM4916',
    'RTBE88U': 'BCM4916',
    'RTBE90U': 'IPQ5322',
    'RTBE92U': 'BCM6765',
    'RTBE9400': 'IPQ5322',
    'RTBE96U': 'BCM4916',
    'RTBE9700': 'BCM6765',
    'TUFAX3000': 'BCM6750',
    'TUFAX3000V2': 'BCM6756',
    'TUFAX4200': 'MT7986A',
    'TUFAX4200Q': 'MT7986A',
    'TUFAX5400': 'BCM6750',
    'TUFAX6000': 'MT7986AV',
    'TUFBE3600': 'BCM6764L',
    'TUFBE3600V1': 'BCM6764L',
    'TUFBE3600V2': 'BCM6764',
    'TUFBE6500': 'IPQ5322',
    'TUFBE9400': 'IPQ5322',
    'TXAX6000': 'MT7986A',
    'XC5': 'BCM6756',
    'XD4': 'BCM6755',
    'XD4PLUS': 'MT7621A',
    'XD4S': 'QCA9557+QCA9563',
    'XD5': 'BCM6756',
    'XD6': 'BCM6750',
    'XD6S': 'QCN5502',
    'XT12': 'BCM4912',
    'XT8': 'BCM6755',
    'XT9': 'BCM6756',
}

def _get_nvram_productid():
    """读取华硕 nvram 商品型号（如 RT-BE88U）；无 nvram 命令/读取失败返回 None"""
    try:
        out = subprocess.check_output(['nvram', 'get', 'productid'], timeout=2, stderr=subprocess.DEVNULL)
        pid = out.decode(errors='ignore').strip()
        return pid or None
    except Exception:
        return None

def get_cpu_model_display():
    """cpu_model 显示策略：
    1. 华硕路由器：nvram productid 命中 ASUS_SOC_MAP -> 返回芯片型号（如 BCM4916）
    2. 未收录型号：返回 'productid (原 lscpu/cpuinfo 结果)'，型号和芯片都展示
    3. 非华硕/读不到 nvram：返回原 get_cpu_model() 结果"""
    pid = _get_nvram_productid()
    fallback = get_cpu_model()
    if pid:
        key = re.sub(r'[^a-zA-Z0-9]', '', pid).upper()
        soc = ASUS_SOC_MAP.get(key)
        if soc:
            return soc
        return '%s (%s)' % (pid, fallback)
    return fallback

def liuliang():
    # 路由器定制：只统计 wan0 接口（路由器 WAN 口，避免把内网/LAN 流量算进去）
    NET_IN = 0
    NET_OUT = 0
    with open('/proc/net/dev') as f:
        for line in f.readlines():
            netinfo = re.findall(r'([^\s]+):[\s]{0,}(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)', line)
            if netinfo:
                if 'wan0' not in netinfo[0][0]:
                    continue
                else:
                    NET_IN += int(netinfo[0][1])
                    NET_OUT += int(netinfo[0][9])
    # 路由器定制：持久化计数器，处理光猫/路由器重启归零
    # 状态: total_in total_out last_raw_in last_raw_out uptime
    # 正常轮: total += 增量; 归零轮(光猫重启): 封口上一段再重新累计
    state_file = '/tmp/mnt/router-usb/entware/serverstatus/.net_counter'
    with open('/proc/uptime') as f:
        uptime = int(float(f.read().split()[0]))
    total_in, total_out = NET_IN, NET_OUT
    try:
        with open(state_file, 'r') as f:
            saved = f.read().strip().split()
            if len(saved) >= 5:
                total_in, total_out, last_raw_in, last_raw_out, prev_uptime = (int(x) for x in saved[:5])
            else:
                total_in, total_out, prev_uptime = (int(x) for x in saved[:3])
                last_raw_in, last_raw_out = total_in, total_out
        if uptime >= prev_uptime:
            if NET_IN < last_raw_in:
                total_in += last_raw_in
            else:
                total_in += NET_IN - last_raw_in
            if NET_OUT < last_raw_out:
                total_out += last_raw_out
            else:
                total_out += NET_OUT - last_raw_out
    except:
        pass
    with open(state_file, 'w') as f:
        f.write(f'{total_in} {total_out} {NET_IN} {NET_OUT} {uptime}')
    return total_in, total_out

def tupd():
    '''
    tcp, udp, process, thread count: for view ddcc attack , then send warning
    :return:
    '''
    s = subprocess.check_output("ss -t|wc -l", shell=True)
    t = int(s[:-1])-1
    s = subprocess.check_output("ss -u|wc -l", shell=True)
    u = int(s[:-1])-1
    s = subprocess.check_output("ps -ef|wc -l", shell=True)
    p = int(s[:-1])-2
    s = subprocess.check_output("ps -eLf|wc -l", shell=True)
    d = int(s[:-1])-2
    return t,u,p,d

def get_network(ip_version):
    # 路由器定制：探测域名改 ipchaxun（大陆可达）
    if(ip_version == 4):
        HOST = "4.ipchaxun.net"
    elif(ip_version == 6):
        HOST = "6.ipchaxun.net"
    try:
        socket.create_connection((HOST, PROBEPORT), 2).close()
        return True
    except:
        return False

_online_status = {4: False, 6: False}
_online_target = 4

def _net_probe_thread():
    """独立线程：每 10s 探测一次 online4/online6，避免 DNS 卡死阻塞主循环上报。
    主循环只读 _online_status 快照；_online_target 由主循环在连接后设置。"""
    global _online_status
    while True:
        target = _online_target
        try:
            _online_status[target] = get_network(target)
        except Exception:
            _online_status[target] = False
        time.sleep(10)

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

    # 路由器定制：resolve DNS every 150 iterations to reduce DNS queries，失败回退缓存 IP
    resolve_count = -1          # init to -1, ensure first iteration resolves DNS
    IP = host
    cached_ip = None            # cache last successful resolved IP for retry
    while True:
        if host.count(':') < 1:  # if not plain ipv6 address, means ipv4 address or hostname
            try:
                if resolve_count % 150 == 0:
                    if PROBE_PROTOCOL_PREFER == 'ipv4':
                        IP = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
                    else:
                        IP = socket.getaddrinfo(host, None, socket.AF_INET6)[0][4][0]
                    cached_ip = IP     # save successful resolved IP
            except Exception:
                if cached_ip:          # use cached IP if DNS fails
                    IP = cached_ip
                # if no cache, keep IP unchanged (socket.create_connection will handle hostname)
            resolve_count = (resolve_count + 1) % 150
            if resolve_count < 0:      # handle initial -1 value
                resolve_count = 0

        if packet_queue.full():
            if packet_queue.get() == 0:
                lostPacket -= 1
        try:
            b = timeit.default_timer()
            socket.create_connection((IP, port), timeout=2).close()
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

def _net_speed():
    while True:
        with open("/proc/net/dev", "r") as f:
            net_dev = f.readlines()
            avgrx = 0
            avgtx = 0
            for dev in net_dev[2:]:
                dev = dev.split(':')
                # 路由器定制：与 liuliang() 一致，只统计 wan0
                if "wan0" not in dev[0]:
                    continue
                dev = dev[1].split()
                avgrx += int(dev[0])
                avgtx += int(dev[8])
            now_clock = time.time()
            netSpeed["diff"] = now_clock - netSpeed["clock"]
            netSpeed["clock"] = now_clock
            netSpeed["netrx"] = int((avgrx - netSpeed["avgrx"]) / netSpeed["diff"])
            netSpeed["nettx"] = int((avgtx - netSpeed["avgtx"]) / netSpeed["diff"])
            netSpeed["avgrx"] = avgrx
            netSpeed["avgtx"] = avgtx
        time.sleep(INTERVAL)

def _disk_io():
    '''
    good luck for opensource! by: cpp.la
    磁盘IO：因为IOPS原因，SSD和HDD、包括RAID卡，ZFS等阵列技术。IO对性能的影响还需要结合自身服务器情况来判断。
    比如我这里是机械硬盘，大量做随机小文件读写，那么很低的读写也就能造成硬盘长时间的等待。
    如果这里做连续性IO，那么普通机械硬盘写入到100Mb/s，那么也能造成硬盘长时间的等待。
    磁盘读写有误差：4k，8k ，https://stackoverflow.com/questions/34413926/psutil-vs-dd-monitoring-disk-i-o
    :return:
    '''
    while True:
        # pre pid snapshot
        snapshot_first = {}
        # next pid snapshot
        snapshot_second = {}
        # read count snapshot
        snapshot_read = 0
        # write count snapshot
        snapshot_write = 0
        # process snapshot
        pid_snapshot = [str(i) for i in os.listdir("/proc") if i.isdigit() is True]
        for pid in pid_snapshot:
            try:
                with open("/proc/{}/io".format(pid)) as f:
                    pid_io = {}
                    for line in f.readlines():
                        if "read_bytes" in line:
                            pid_io["read"] = int(line.split("read_bytes:")[-1].strip())
                        elif "write_bytes" in line and "cancelled_write_bytes" not in line:
                            pid_io["write"] = int(line.split("write_bytes:")[-1].strip())
                    pid_io["name"] = open("/proc/{}/comm".format(pid), "r").read().strip()
                    snapshot_first[pid] = pid_io
            except:
                if pid in snapshot_first:
                    snapshot_first.pop(pid)

        time.sleep(INTERVAL)

        for pid in pid_snapshot:
            try:
                with open("/proc/{}/io".format(pid)) as f:
                    pid_io = {}
                    for line in f.readlines():
                        if "read_bytes" in line:
                            pid_io["read"] = int(line.split("read_bytes:")[-1].strip())
                        elif "write_bytes" in line and "cancelled_write_bytes" not in line:
                            pid_io["write"] = int(line.split("write_bytes:")[-1].strip())
                    pid_io["name"] = open("/proc/{}/comm".format(pid), "r").read().strip()
                    snapshot_second[pid] = pid_io
            except:
                if pid in snapshot_first:
                    snapshot_first.pop(pid)
                if pid in snapshot_second:
                    snapshot_second.pop(pid)

        for k, v in snapshot_first.items():
            if snapshot_first[k]["name"] == snapshot_second[k]["name"] and snapshot_first[k]["name"] != "bash":
                snapshot_read += (snapshot_second[k]["read"] - snapshot_first[k]["read"])
                snapshot_write += (snapshot_second[k]["write"] - snapshot_first[k]["write"])
        diskIO["read"] = snapshot_read
        diskIO["write"] = snapshot_write

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
        target=_net_speed,
    )
    t5 = threading.Thread(
        target=_disk_io,
    )
    t6 = threading.Thread(
        target=_net_probe_thread,
    )
    for ti in [t1, t2, t3, t4, t5, t6]:
        ti.daemon = True
        ti.start()


def _monitor_thread(name, host, interval, type):
    while True:
        if name not in monitorServer.keys():
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

            # 2) 解析 IP（按偏好族）
            IP = addr
            if addr.count(':') < 1:  # 非纯 IPv6
                try:
                    if PROBE_PROTOCOL_PREFER == 'ipv4':
                        IP = socket.getaddrinfo(addr, None, socket.AF_INET)[0][4][0]
                    else:
                        IP = socket.getaddrinfo(addr, None, socket.AF_INET6)[0][4][0]
                except Exception:
                    pass

            # 3) 建连耗时（timeout=1s），ECONNREFUSED 也计入
            try:
                b = timeit.default_timer()
                socket.create_connection((IP, port), timeout=1).close()
                monitorServer[name]["latency"] = int((timeit.default_timer() - b) * 1000)
            except socket.error as error:
                if getattr(error, 'errno', None) == errno.ECONNREFUSED:
                    monitorServer[name]["latency"] = int((timeit.default_timer() - b) * 1000)
                else:
                    monitorServer[name]["latency"] = 0
        except Exception:
            monitorServer[name]["latency"] = 0
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
    while True:
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
                monitorServer.clear()
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
            CPUModel = get_cpu_model_display()
            while True:
                CPU = get_cpu()
                NET_IN, NET_OUT = liuliang()
                Uptime = get_uptime()
                Load_1, Load_5, Load_15 = os.getloadavg()
                MemoryTotal, MemoryUsed, SwapTotal, SwapFree = get_memory()
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
                array['swap_used'] = SwapTotal - SwapFree
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
                    if sysname.startswith('linux'):
                        os_name = 'linux'
                        # try distro from os-release
                        try:
                            with open('/etc/os-release') as f:
                                for line in f:
                                    if line.startswith('ID='):
                                        val = line.strip().split('=',1)[1].strip().strip('"')
                                        if val: os_name = val
                                        break
                        except Exception:
                            pass
                    elif sysname.startswith('darwin'):
                        os_name = 'darwin'
                    elif sysname.startswith('freebsd'):
                        os_name = 'freebsd'
                    elif sysname.startswith('openbsd'):
                        os_name = 'openbsd'
                    elif sysname.startswith('netbsd'):
                        os_name = 'netbsd'
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
