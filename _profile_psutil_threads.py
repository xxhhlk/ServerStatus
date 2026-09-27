#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
按线程拆解 client-psutil.py 的 CPU 占用（Windows 读原生线程描述，Linux 读 /proc）

用法:
    python _profile_psutil_threads.py [-p PID] [-s 采样秒数]
    默认自动定位正在运行的 client-psutil.py（-p 可显式指定）；找到多个候选会列出来并要求指定。
    注意：读别的进程命令行需要 PROCESS_VM_READ，客户端若以管理员运行、本脚本没提权就读不到，
          此时会自动列出「命令行读不到的进程」并提示改用管理员终端或 -p。

自动定位：先只保留进程名含 python 的进程（排除 shell 包装器——它的命令行里也会带目标脚本名），
再取命令行里第一个 .py token 作为「被执行的脚本」比对。找不到时会列出命令行读不到的 python
进程（通常是权限问题）以及其它 python 进程，便于判断原因。

输出: 每个线程在采样窗口内消耗的 CPU 时间，按线程名归类。线程名由客户端
      _name_current_thread() 写入（Windows 原生线程描述 / Linux comm）；
      若全显示 (未命名)，说明跑的客户端版本还没有那个函数。
"""
import argparse
import os
import sys
import time

try:
    import psutil
except ImportError:
    sys.exit("需要 psutil：pip install psutil")

TARGET = "client-psutil.py"      # 自动定位时匹配的脚本名，Linux 上可改成 client-linux.py
DEFAULT_SECONDS = 120            # Windows 线程 CPU 按 15.6ms 调度节拍计账，窗口太短会大量显示 0
MIN_REPORT = 0.0005              # 小于 0.5ms 的线程不单列


def _script_arg(cmdline):
    """取命令行里第一个 .py token —— 即真正被执行的脚本（参数里同名的路径不算）"""
    for arg in cmdline:
        token = arg.strip().strip('"\'')
        if token.lower().endswith(".py"):
            return token
    return None


def find_client_pids():
    """返回 (命中, 命令行读不到的 python 进程, 所有 python 进程)

    只考虑进程名含 python 的进程 —— shell 包装器（如 `bash -c '... python client-psutil.py'`）
    的命令行里也会出现目标脚本名，不先按进程名过滤会把它误报成客户端。
    读别的进程命令行需要 PROCESS_VM_READ，目标以管理员/更高完整性级别运行时会被拒，
    所以单独把「读不到的 python 进程」列出来，否则只看到一句「没找到」无从判断原因。
    """
    me = os.getpid()
    hits, denied, pythons = [], [], []
    for proc in psutil.process_iter():
        try:
            if proc.pid == me:
                continue
            pname = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "python" not in pname.lower():
            continue
        try:
            cmdline = proc.cmdline()
        except psutil.AccessDenied:
            denied.append((proc.pid, pname))
            continue
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        pythons.append((proc.pid, pname, cmdline))
        script = _script_arg(cmdline)
        if script and os.path.basename(script).lower() == TARGET.lower():
            hits.append((proc.pid, pname, cmdline))
    return hits, denied, pythons


def make_name_reader(pid):
    """返回 tid -> 线程名 的读取函数（读不到返回空串）"""
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.windll.kernel32
        k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.OpenThread.restype = wintypes.HANDLE
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.GetThreadDescription.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
        k32.GetThreadDescription.restype = ctypes.c_long

        def name_of(tid):
            handle = k32.OpenThread(0x0800, False, tid)   # THREAD_QUERY_LIMITED_INFORMATION
            if not handle:
                return ""
            try:
                buf = ctypes.c_wchar_p()
                k32.GetThreadDescription(handle, ctypes.byref(buf))
                return buf.value or ""
            finally:
                k32.CloseHandle(handle)

        return name_of

    def name_of(tid):   # Linux 等：线程名在 comm 里（Python 的 Thread(name=) 会写）
        try:
            with open("/proc/%d/task/%d/comm" % (pid, tid)) as f:
                return f.read().strip()
        except Exception:
            return ""

    return name_of


def main():
    parser = argparse.ArgumentParser(description="按线程拆解 %s 的 CPU 占用" % TARGET)
    parser.add_argument("-p", "--pid", type=int, help="客户端进程号；不给则自动定位")
    parser.add_argument("-s", "--seconds", type=int, default=DEFAULT_SECONDS,
                        help="采样秒数（默认 %d）" % DEFAULT_SECONDS)
    args = parser.parse_args()
    pid, seconds = args.pid, args.seconds

    if pid is None:
        hits, denied, pythons = find_client_pids()
        if not hits:
            print("没找到正在运行的 %s。" % TARGET)
            if denied:
                print("\n有 %d 个 python 进程的命令行读不到 —— 多半是它以管理员/更高完整性级别"
                      "运行，而本脚本没提权：" % len(denied))
                for p, pname in denied:
                    print("  %-7d %s" % (p, pname))
                print("  解决：以管理员身份重开终端跑本脚本；或从任务管理器取 PID 后用 -p 指定。")
            if pythons:
                print("\n本机其它 python 进程（命令行里第一个 .py 不是 %s，供对照）：" % TARGET)
                for p, pname, cmdline in pythons:
                    print("  %-7d %-14s %s" % (p, pname, " ".join(cmdline)))
            if not denied and not pythons:
                print("也没发现任何 python 进程。")
            print("\n可用 -p 显式指定：python %s -p <PID>" % os.path.basename(__file__))
            sys.exit(1)
        if len(hits) > 1:
            print("找到 %d 个候选，请用 -p 显式指定：" % len(hits))
            for p, pname, cmdline in hits:
                print("  %-7d %-16s %s" % (p, pname, " ".join(cmdline)))
            sys.exit(1)
        pid = hits[0][0]
        print("自动定位到 %s：pid=%d" % (TARGET, pid))

    try:
        proc = psutil.Process(pid)
        pname = proc.name()
        p0 = proc.cpu_times()
    except psutil.NoSuchProcess:
        sys.exit("pid %d 不存在" % pid)

    print("目标：pid=%d (%s)   采样 %d 秒 ..." % (pid, pname, seconds))

    # 顺便把客户端看到的关键环境变量打出来：配置类改动（如 TUP_INTERVAL）是否生效，
    # 光看线程 CPU 判断不出来（Windows 线程计费是节拍采样，会少记/多记）。
    try:
        env = proc.environ()
    except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
        print("（读不到客户端环境变量：%s，多半是权限不足）" % type(exc).__name__)
    else:
        # Windows 上 psutil 读回来的键名会被大写（进程内 os.environ 则是原样、
        # 且查找本身不区分大小写），所以这里必须按大小写不敏感来比对，
        # 否则会把已经生效的 serverstatus_TUP_INTERVAL 误报成"没传进去"。
        upper = {}
        for k, v in env.items():
            upper.setdefault(k.upper(), (k, v))
        keys = sorted(u for u in upper if u.startswith("SERVERSTATUS_"))
        if keys:
            print("客户端环境变量：")
            for u in keys:
                raw, val = upper[u]
                print("  %-34s = %s" % (raw, val))
        else:
            print("客户端环境变量：没有任何 serverstatus_* 项（配置没传进去）")
        # 常见误配：漏了 serverstatus_ 前缀，客户端只认带前缀的名字
        for bare in ("SERVER", "PORT", "USER", "PASSWORD", "INTERVAL",
                     "TUP_INTERVAL", "DNS_REFRESH_INTERVAL", "NET_PROBE_INTERVAL"):
            if bare in upper and ("SERVERSTATUS_" + bare) not in upper:
                print("  注意：发现 %s 但没有 serverstatus_%s —— 前者不生效" % (bare, bare))

    def snap():
        out = {}
        try:
            for t in proc.threads():
                out[t.id] = t.user_time + t.system_time
        except psutil.NoSuchProcess:
            pass
        return out

    first = snap()
    t0 = time.perf_counter()
    time.sleep(seconds)
    t1 = time.perf_counter()
    last = snap()
    try:
        p1 = proc.cpu_times()
    except psutil.NoSuchProcess:
        sys.exit("pid %d 在采样期间退出" % pid)

    window = t1 - t0
    process_cpu = sum(p1[:2]) - sum(p0[:2])
    rows = sorted(((last.get(tid, 0.0) - first.get(tid, 0.0), tid)
                   for tid in set(first) | set(last)), reverse=True)

    name_of = make_name_reader(pid)
    print()
    print("%-18s %7s %10s %10s" % ("线程名", "tid", "CPU(s)", "占本进程"))
    thread_sum = 0.0
    for delta, tid in rows:
        if delta < MIN_REPORT:
            continue
        share = (delta / process_cpu * 100) if process_cpu > 0 else 0.0
        print("%-18s %7d %10.3f %9.1f%%" % (name_of(tid) or "(未命名)", tid, delta, share))
        thread_sum += delta
    leftover = process_cpu - thread_sum
    if leftover > MIN_REPORT:
        share = (leftover / process_cpu * 100) if process_cpu > 0 else 0.0
        print("%-18s %7s %10.3f %9.1f%%" % ("(未计入任何线程)", "", leftover, share))
    print("-" * 48)
    print("%-18s %7s %10.3f %9.2f%% 单核"
          % ("本进程合计", "", process_cpu, process_cpu / window * 100))
    print("\n提示：")
    print("  · 只统计「已连接的客户端」——没连上服务端时主循环不跑，只能看到后台线程。")
    print("  · Windows 线程 CPU 按 15.6ms 调度节拍计账，显示 0 只代表「窗口内不足一拍」；")
    print("    低占用线程请用 -s 300 这类长窗口再看。")
    print("  · monitor-* 是服务端下发的监控目标，每个按服务端 interval 独立建连。")


if __name__ == "__main__":
    main()
