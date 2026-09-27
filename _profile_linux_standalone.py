#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client-linux.py 耗时 + CPU 占用诊断脚本（在真实 Linux 环境运行，不连接服务器）

用法: python3 _profile_linux_standalone.py [client-linux.py 路径] [-s 采样秒数]

输出:
  [0] 环境与规模基线（进程数/线程数/网卡数/挂载点数）
  [1] 每轮上报路径的纯函数 micro-bench（墙钟 + 本进程 CPU 增量）
  [2] tupd() 现状（ss/ps 子进程）vs 直读 /proc 的对照（计数一致性 + 成本）
  [3] 常驻循环线程的 per-cycle CPU（_disk_io / _net_speed）
  [4] 活客户端按线程拆解（Linux 线程无名字，按 tid 升序 = 创建顺序推断）

注意: 墙钟耗时含等待（IO/网络/sleep），不等于 CPU 占用。
      判断 CPU 看 CPU 列；CPU/墙钟 接近 100% 才是真占 CPU，低占比说明大部分时间在等。
"""
import importlib.util
import os
import platform
import re
import resource
import subprocess
import sys
import threading
import time

try:
    import psutil
except ImportError:
    psutil = None


def load_mod(target):
    spec = importlib.util.spec_from_file_location("clinux", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cpu_now():
    """本进程累计 CPU 秒（user+system）。用 rusage，微秒级分辨率，micro-bench 才测得动。"""
    if psutil is not None:
        t = psutil.Process().cpu_times()
        return t.user + t.system
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def cpu_children():
    """已回收子进程累计 CPU 秒。ss/ps 这类 shell=True 调用的 CPU 记在子进程上，
    用 cpu_now() 会显示 ~0——必须分开量，否则会误判 tupd「不耗 CPU」。"""
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def bench_child(fn, n=5):
    """子进程 CPU（ms/次）。"""
    c0 = cpu_children()
    for _ in range(n):
        fn()
    return (cpu_children() - c0) / n * 1000


def bench(fn, min_wall=0.35, max_iter=200000, budget=2.5):
    """返回 (墙钟ms, CPU ms, 错误)。

    连测两次：第 1 次用于判错，第 2 次代表稳态——首次调用常有冷启动（缓存/惰性初始化），
    只看首次会把稳态开销严重高估。若第 2 次仍 >= min_wall（子进程、真慢调用），直接作为结果
    返回、不再重复；否则批量迭代到累计墙钟 >= min_wall，保证 CPU 增量可测。
    CPU 取本进程 user+system 增量——阻塞等待的时间不计入，这是与墙钟的关键区别。
    """
    for _ in range(2):
        try:
            c0 = cpu_now()
            w0 = time.perf_counter()
            fn()
        except Exception as e:
            return None, None, type(e).__name__ + ": " + str(e)[:80]
    w1 = time.perf_counter()
    c1 = cpu_now()
    n = 1
    if w1 - w0 < min_wall:
        batch = 1 if (w1 - w0) > 0.005 else 64
        while True:
            for _ in range(batch):
                try:
                    fn()
                except Exception:
                    pass
            n += batch
            elapsed = time.perf_counter() - w0
            if elapsed >= min_wall or n >= max_iter or elapsed >= budget:
                break
    w2 = time.perf_counter()
    c2 = cpu_now()
    return ((w2 - w0) / n * 1000, (c2 - c0) / n * 1000, None)


class _Stop(Exception):
    pass


_STOP = threading.Event()
_CYCLES = [0]


class _FakeTime(object):
    """只替换客户端模块里的 time.sleep（换成「计数 + 空转」），其余属性透传。

    注意：替换的是客户端模块 globals 里的 time，不是全局 time 模块——否则本脚本
    自己的 sleep 也会被顶掉。收线程时先置 _STOP，等线程在下一次 sleep 抛 _Stop 退出，
    再还原 globals，避免它在还原后调到真 sleep 而永久挂着。
    """

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)

    def sleep(self, _seconds):
        if _STOP.is_set():
            raise _Stop
        _CYCLES[0] += 1


def bench_loop(fn, seconds=5.0):
    """给「while True 里 sleep(INTERVAL)」的常驻线程测 per-cycle CPU。

    跑 seconds 秒后 CPU 增量 / 完成轮数 = 单轮 CPU。只启动目标线程，主线程只在真
    sleep（≈0 CPU）。返回 (CPU ms/轮, 轮数, 错误)。
    """
    import time as real_time
    g = fn.__globals__
    saved = g.get('time')
    _CYCLES[0] = 0
    _STOP.clear()
    g['time'] = _FakeTime(real_time)
    t = None
    cycles = 0
    err = None

    def runner():
        try:
            fn()
        except _Stop:
            pass
        except Exception:
            pass

    try:
        c0 = cpu_now()
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        real_time.sleep(seconds)
        c1 = cpu_now()
        cycles = _CYCLES[0]
    except Exception as e:
        err = type(e).__name__ + ": " + str(e)[:80]
    finally:
        _STOP.set()
        if t is not None:
            try:
                t.join(timeout=10)
            except Exception:
                pass
        if saved is not None:
            g['time'] = saved
    if err:
        return None, cycles, err
    if cycles == 0:
        return None, 0, "0 轮（sleep 未被调用？）"
    return (c1 - c0) * 1000 / cycles, cycles, None


def fmt(name, r):
    wall, cpu, err = r
    if err:
        return f"{name:40s} FAIL    {err}"
    ratio = (cpu / wall * 100) if wall > 0 else 0.0
    return f"{name:40s} {wall:9.3f} {cpu:9.3f} {ratio:8.0f}%"


HEAD = f"{'函数':40s} {'墙钟ms':>9s} {'CPU ms':>9s} {'CPU/墙钟':>9s}"


# --- 直读 /proc 的候选实现（与 router 版 client-linux-router.py 同口径） ---------
def _count_sockets(paths, keep=(), exclude=()):
    n = 0
    for path in paths:
        try:
            with open(path) as f:
                next(f, None)  # 表头
                for line in f:
                    parts = line.split()
                    if len(parts) < 4:
                        continue
                    if parts[3] in exclude or (keep and parts[3] not in keep):
                        continue
                    n += 1
        except IOError:
            pass
    return n


def _count_processes():
    try:
        return sum(1 for d in os.listdir('/proc') if d.isdigit())
    except OSError:
        return 0


def _count_threads_task():
    try:
        pids = [d for d in os.listdir('/proc') if d.isdigit()]
    except OSError:
        return 0
    total = 0
    for pid in pids:
        try:
            total += sum(1 for d in os.listdir('/proc/' + pid + '/task') if d.isdigit())
        except OSError:
            pass
    return total


def _threads_from_loadavg():
    try:
        with open('/proc/loadavg') as f:
            return int(f.read().split()[3].split('/')[1])
    except Exception:
        return -1


def _sh(cmd):
    return int(subprocess.check_output(cmd, shell=True)[:-1])


def find_client_pids():
    hits = []
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            with open('/proc/%s/cmdline' % d, 'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode(errors='ignore')
        except OSError:
            continue
        if 'client-linux' in cmd and '.py' in cmd:
            hits.append((int(d), cmd.strip()))
    return sorted(hits)


def section_env(mod, target):
    print(f"== 环境: {platform.platform()} | python {platform.python_version()} | "
          f"psutil {psutil.__version__ if psutil else 'N/A'}")
    print(f"== 分析目标: {os.path.abspath(target)}")
    try:
        with open('/proc/loadavg') as f:
            print(f"== loadavg: {f.read().strip()}")
    except OSError:
        pass
    print(f"== 进程数(/proc 数字目录): {_count_processes()}   "
          f"线程数(/proc/*/task 累加): {_count_threads_task()}   "
          f"线程数(/proc/loadavg 第4段): {_threads_from_loadavg()}")
    ifaces = []
    try:
        ifaces = sorted(os.listdir('/sys/class/net'))
    except OSError:
        pass
    print(f"== 网卡({len(ifaces)}): {ifaces}")
    print(f"== 物理网卡: {[n for n in ifaces if mod._is_physical_interface(n)]}")
    try:
        with open('/proc/mounts') as f:
            mounts = [ln.split() for ln in f if len(ln.split()) >= 3]
        valid = {"ext4", "ext3", "ext2", "reiserfs", "jfs", "btrfs", "fuseblk",
                 "zfs", "simfs", "ntfs", "fat32", "exfat", "xfs"}
        picked = sorted({m[1] for m in mounts if m[2].lower() in valid})
        print(f"== 挂载点总数: {len(mounts)}   get_hdd 会 statvfs 的({len(picked)}): {picked}")
    except OSError:
        pass
    print(f"== 客户端 INTERVAL={mod.INTERVAL} DNS_REFRESH_INTERVAL={mod.DNS_REFRESH_INTERVAL} "
          f"NET_PROBE_INTERVAL={mod.NET_PROBE_INTERVAL}")


def section_functions(mod):
    print("\n[1] 每轮上报路径的纯函数（主循环每秒调用一次）")
    print(HEAD)
    print(fmt("get_uptime(/proc/uptime)", bench(mod.get_uptime)))
    print(fmt("get_time(读一次 /proc/stat)", bench(mod.get_time)))
    print(fmt("get_memory(/proc/meminfo + 正则)", bench(mod.get_memory)))
    print(fmt("get_hdd(/proc/mounts + statvfs)", bench(mod.get_hdd)))
    print(fmt("get_os_name(platform+/etc/os-release)", bench(mod.get_os_name)))
    print(fmt("os.getloadavg", bench(lambda: os.getloadavg())))
    print(fmt("liuliang(/proc/net/dev)", bench(mod.liuliang)))
    iface = None
    for n in sorted(os.listdir('/sys/class/net')):
        if mod._is_physical_interface(n):
            iface = n
            break
    if iface:
        print(fmt(f"_is_physical_interface({iface!r})", bench(lambda: mod._is_physical_interface(iface))))
    print(fmt("get_cpu_cores(只连接时调一次)", bench(mod.get_cpu_cores)))


def section_tupd(mod):
    print("\n[2] tupd() 成本 + 各计数口径对照")
    print(HEAD)
    print(fmt("tupd(客户端实际路径)", bench(mod.tupd)))
    for cmd in ("ss -t|wc -l", "ss -u|wc -l", "ps -ef|wc -l", "ps -eLf|wc -l"):
        print(fmt("  legacy: " + cmd, bench(lambda c=cmd: _sh(c))))
    print("  -- 子进程 CPU（RUSAGE_CHILDREN，客户端自身看不到这部分）--")
    print(f"  {'命令':40s} {'子进程CPU ms/次':>16s}")
    for cmd in ("ss -t|wc -l", "ss -u|wc -l", "ps -ef|wc -l", "ps -eLf|wc -l"):
        print(f"  {'legacy: ' + cmd:40s} {bench_child(lambda c=cmd: _sh(c)):16.3f}")
    print(f"  {'tupd(客户端实际路径)':40s} {bench_child(mod.tupd):16.3f}")
    print(HEAD)
    print(fmt("候选: tcp 直读 /proc/net/tcp{,6}", bench(
        lambda: _count_sockets(('/proc/net/tcp', '/proc/net/tcp6'), exclude=('03', '06', '0A')))))
    print(fmt("候选: udp 直读 /proc/net/udp{,6}", bench(
        lambda: _count_sockets(('/proc/net/udp', '/proc/net/udp6'), keep=('01',)))))
    print(fmt("候选: 进程数 os.listdir('/proc')", bench(_count_processes)))
    print(fmt("候选: 线程数 /proc/*/task 累加", bench(_count_threads_task)))
    print(fmt("候选: 线程数 /proc/loadavg 第4段", bench(_threads_from_loadavg)))
    # 计数一致性（现状 vs 候选）——改口径前必须逐项对齐。取 3 次采样，避免瞬时抖动误判。
    print("\n  计数对照（各 3 次，现状/候选交替采样；括号内为 3 次值）：")
    pairs = [
        ("tcp", lambda: _sh("ss -t|wc -l") - 1,
         lambda: _count_sockets(('/proc/net/tcp', '/proc/net/tcp6'), exclude=('03', '06', '0A'))),
        ("udp", lambda: _sh("ss -u|wc -l") - 1,
         lambda: _count_sockets(('/proc/net/udp', '/proc/net/udp6'), keep=('01',))),
        ("process", lambda: _sh("ps -ef|wc -l") - 2, _count_processes),
        ("thread", lambda: _sh("ps -eLf|wc -l") - 2, _count_threads_task),
        ("thread(loadavg)", lambda: _sh("ps -eLf|wc -l") - 2, _threads_from_loadavg),
    ]
    for name, cur, new in pairs:
        cs = [cur() for _ in range(3)]
        ns = [new() for _ in range(3)]
        flag = "OK " if abs(sum(cs) / 3 - sum(ns) / 3) <= 1 else "差异"
        print(f"    {name:16s} 现状={str(cs):20s} 候选={str(ns):20s} {flag}")


def _scan_pass(comm=True):
    """_disk_io 的一轮全量扫描（与客户端同路径：/proc/<pid>/io 必读，comm 可选）。

    客户端现在是「每轮两遍 × (io + comm)」；这里拆开量，好算单轮化 / 去掉 comm 各能省多少。
    """
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            with open('/proc/%s/io' % d) as f:
                for line in f:
                    if 'read_bytes' in line or 'write_bytes' in line:
                        pass
        except OSError:
            continue
        if comm:
            try:
                with open('/proc/%s/comm' % d) as f:
                    f.read()
            except OSError:
                pass


def section_loops(mod):
    print("\n[3] 常驻循环线程 per-cycle CPU（sleep 置空转、跑固定秒数再除以轮数）")
    print(f"{'线程':40s} {'CPU ms/轮':>10s} {'轮数':>10s} {'CPU/轮墙钟':>12s}")
    window = 5.0
    for name, fn in (("_disk_io(现状: 两遍 io+comm)", mod._disk_io),
                     ("_net_speed(/proc/net/dev)", mod._net_speed)):
        cpu_ms, cycles, err = bench_loop(fn, seconds=window)
        if err:
            print(f"{name:40s} FAIL    {err}")
            continue
        per_cycle_wall = window * 1000.0 / cycles if cycles else 0
        print(f"{name:40s} {cpu_ms:10.3f} {cycles:10d} {per_cycle_wall:11.3f}ms")

    print("\n  _disk_io 候选拆解（单轮扫描成本；现状一轮 = 两遍）：")
    print(HEAD)
    print(fmt("  单遍 io+comm", bench(lambda: _scan_pass(True))))
    print(fmt("  单遍 io(comm 走缓存)", bench(lambda: _scan_pass(False))))


def _task_cpu_direct(pid):
    """不依赖 psutil 的线程 CPU 快照：直接解析 /proc/<pid>/task/<tid>/stat。

    /proc/pid/stat 的 comm 可能含空格与 ')'，故先按最后一个 ')' 切开，再数偏移：
    切开后第 0 个 token 是 state(field 3)，utime/stime 是 field 14/15 → 下标 11/12。
    """
    hz = os.sysconf('SC_CLK_TCK')
    out = {}
    for tid in os.listdir('/proc/%d/task' % pid):
        try:
            with open('/proc/%d/task/%s/stat' % (pid, tid)) as f:
                after = f.read().rsplit(')', 1)[1].split()
            out[int(tid)] = (int(after[11]) + int(after[12])) / hz
        except (OSError, IndexError, ValueError):
            pass
    return out


def _proc_total_cpu_direct(pid):
    hz = os.sysconf('SC_CLK_TCK')
    try:
        with open('/proc/%d/stat' % pid) as f:
            after = f.read().rsplit(')', 1)[1].split()
        return (int(after[11]) + int(after[12])) / hz
    except (OSError, IndexError, ValueError):
        return 0.0


def section_live_client(seconds):
    pids = find_client_pids()
    print(f"\n[4] 活客户端按线程拆解（采样 {seconds}s）")
    if not pids:
        print("  没找到正在运行的 client-linux.py（跳过）")
        return
    # Linux 线程无名字（comm 都是 python3），按 tid 升序 = 创建顺序推断：
    #   最小 tid == pid 是主线程；随后依次是 get_realtime_data() 里 t1..t6；
    #   再往后是服务端下发监控目标时创建的 _monitor_thread。
    ORDER = ["主线程", "ping-10010", "ping-189", "ping-10086",
             "net_speed", "disk_io", "net_probe"]
    for pid, cmd in pids:
        if pid == os.getpid():
            continue
        try:
            first = _task_cpu_direct(pid)
            total0 = _proc_total_cpu_direct(pid)
        except Exception as e:
            print(f"  pid={pid} 读不到: {type(e).__name__}")
            continue
        w0 = time.perf_counter()
        time.sleep(seconds)
        w1 = time.perf_counter()
        last = _task_cpu_direct(pid)
        total1 = _proc_total_cpu_direct(pid)
        if not first or not last:
            print(f"  pid={pid} 线程快照为空（跳过）")
            continue
        window = w1 - w0
        total = total1 - total0
        tids = sorted(set(first) | set(last))
        print(f"  pid={pid}  {cmd}")
        if tids and tids[0] != pid:
            print(f"    注意: 最小 tid={tids[0]} != pid={pid}，tid 升序推断可能不准")
        print(f"    {'线程(推断)':18s} {'tid':>9s} {'CPU(s)':>9s} {'占本进程':>9s} {'ms/s':>8s}")
        rows = sorted(((last.get(t, 0.0) - first.get(t, 0.0), t)
                       for t in set(first) | set(last)), reverse=True)
        for i, (delta, tid) in enumerate(sorted(rows, key=lambda x: x[1])):
            if delta < 0.0005:
                continue
            name = ORDER[i] if i < len(ORDER) else ("monitor-%d" % (i - len(ORDER)))
            share = (delta / total * 100) if total > 0 else 0.0
            print(f"    {name:18s} {tid:9d} {delta:9.3f} {share:8.1f}% {delta / window * 1000:8.3f}")
        print(f"    {'-- 本进程合计':18s} {'':>9s} {total:9.3f} {'':>9s} "
              f"{total / window * 1000:8.3f}  ({total / window * 100:5.2f}% 单核)")


def main():
    target = "client-linux.py"
    seconds = 120
    args = sys.argv[1:]
    if "-s" in args:
        i = args.index("-s")
        seconds = int(args[i + 1])
        del args[i:i + 2]
    if args:
        target = args[0]
    if not os.path.exists(target):
        print(f"找不到文件: {target}")
        sys.exit(1)
    mod = load_mod(target)
    section_env(mod, target)
    section_functions(mod)
    section_tupd(mod)
    section_loops(mod)
    section_live_client(seconds)
    print("\n== 完成。把以上输出贴回来即可分析。 ==")


if __name__ == "__main__":
    main()
