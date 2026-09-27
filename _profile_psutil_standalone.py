#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client-psutil.py 耗时 + CPU 占用诊断脚本（在真实环境运行，不连接服务器）
用法: python _profile_psutil_standalone.py [client-psutil.py 路径]
      默认找当前目录 client-psutil.py；不填路径时也尝试 OneDrive 副本位置。
输出: 各热点函数单次平均「墙钟耗时」与「进程自身 CPU 时间」+ Windows 兼容性检查

注意: 墙钟耗时含等待（IO/网络/锁/sleep），不等于 CPU 占用。
      判断 CPU 开销看 CPU 列；CPU/墙钟 接近 100% 才是真占 CPU，低占比说明大部分时间在等。
"""
import importlib.util, time, sys, os, platform
import psutil

def load_mod(target):
    spec = importlib.util.spec_from_file_location("cps", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def bench(fn, min_wall=0.35, max_iter=200000, budget=2.5):
    """返回 (墙钟ms, CPU ms, 错误)。
    连测两次：第 1 次用于判错，第 2 次代表稳态——首次调用常有冷启动（如 psutil.swap_memory
    首次解析 PDH 计数器路径可达数百毫秒），只看首次会把稳态开销严重高估。
    若第 2 次仍 >= min_wall（PowerShell、网络等真慢调用），直接作为结果返回、不再重复。
    否则批量迭代到累计墙钟 >= min_wall（或预算用尽），保证 CPU 增量可测。
    CPU 取本进程 user+system 增量——阻塞等待的时间不计入，这是与墙钟的关键区别。"""
    proc = psutil.Process()
    for _ in range(2):
        try:
            c0 = proc.cpu_times(); w0 = time.perf_counter()
            fn()
        except Exception as e:
            return None, None, type(e).__name__ + ": " + str(e)[:80]
    w1 = time.perf_counter(); c1 = proc.cpu_times()
    n = 1
    if w1 - w0 < min_wall:
        batch = 1 if (w1 - w0) > 0.005 else 64  # 中等耗时逐个跑，极快调用批量跑，避免计时器开销污染
        while True:
            for _ in range(batch):
                try: fn()
                except Exception: pass
            n += batch
            elapsed = time.perf_counter() - w0
            if elapsed >= min_wall or n >= max_iter or elapsed >= budget:
                break
    w2 = time.perf_counter(); c2 = proc.cpu_times()
    return ((w2 - w0) / n * 1000,
            ((c2.user - c0.user) + (c2.system - c0.system)) / n * 1000, None)

def fmt(name, r):
    wall, cpu, err = r
    if err: return f"{name:40s} FAIL    {err}"
    ratio = (cpu / wall * 100) if wall > 0 else 0.0
    return f"{name:40s} {wall:9.3f} {cpu:9.3f} {ratio:8.0f}%"

HEAD = f"{'函数':40s} {'墙钟ms':>9s} {'CPU ms':>9s} {'CPU/墙钟':>9s}"

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "client-psutil.py"
    if not os.path.exists(target):
        alt = os.path.expandvars(r"%USERPROFILE%\OneDrive\文档\client-psutil.py")
        if os.path.exists(alt): target = alt
        else:
            print(f"找不到文件: {target}"); sys.exit(1)
    print(f"== 环境: {platform.platform()} | python {platform.python_version()} | "
          f"psutil {psutil.__version__}")
    print(f"== 分析目标: {os.path.abspath(target)}")
    mod = load_mod(target)

    print("\n[1] 系统信息类（每轮上报都会调用）")
    print(HEAD)
    print(fmt("get_uptime(客户端实际路径)", bench(mod.get_uptime)))
    print(fmt("get_memory(客户端实际路径)", bench(mod.get_memory)))
    print(fmt("get_swap(客户端实际路径)", bench(mod.get_swap)))
    print(fmt("get_hdd(客户端实际路径)", bench(mod.get_hdd)))
    print(fmt("get_os_name(/etc/os-release)", bench(mod.get_os_name)))
    print(fmt("get_cpu_model", bench(mod.get_cpu_model)))
    print(fmt("os.getloadavg(仅linux/darwin用)", bench(lambda: os.getloadavg())))

    print("\n[2] 网络热点")
    print(HEAD)
    # _win_build_map 内部无缓存，每次调用都真的起 PowerShell（慢，bench 会只跑一次）
    print(fmt("_win_build_map(每次起 PowerShell)", bench(mod._win_build_map)))
    try:
        nics = list(psutil.net_io_counters(pernic=True).keys())
    except Exception:
        nics = []
    print(f"网卡列表: {nics}")
    # 先建好 PNPDeviceID 映射，下面测的才是 is_virtual_nic 的稳态开销（一次性成本由上一行体现）
    if nics:
        try: mod.is_virtual_nic(nics[0])
        except Exception: pass
    for n in nics:
        print(fmt(f"is_virtual_nic({n!r})", bench(lambda n=n: mod.is_virtual_nic(n))))
    print(fmt("psutil.net_io_counters(pernic=True)[对照]", bench(lambda: psutil.net_io_counters(pernic=True))))
    if hasattr(mod, "_net_io_counters"):
        print(fmt("_net_io_counters(客户端实际用)", bench(mod._net_io_counters)))
    def one_net_round():
        net = mod._net_io_counters() if hasattr(mod, "_net_io_counters") else psutil.net_io_counters(pernic=True)
        ti = to = 0
        for k, v in net.items():
            if mod.is_virtual_nic(k): continue
            ti += v[1]; to += v[0]
        return ti, to
    print(fmt("完整一轮采样(计数+虚拟过滤)", bench(one_net_round)))
    print(fmt("get_network(连接80端口)", bench(lambda: mod.get_network(4))))

    print("\n[3] 子进程/原生调用类")
    print(HEAD)
    print(fmt("tupd(平台分支: win原生/linux ss)", bench(mod.tupd)))
    if sys.platform.startswith('win') and hasattr(mod, '_win_sys_counts'):
        if hasattr(mod, '_win_tcp_conn_count'):
            print(fmt("_win_tcp_conn_count(客户端实际用)", bench(mod._win_tcp_conn_count)))
        print(fmt("psutil.net_connections(tcp)[对照]", bench(lambda: psutil.net_connections(kind='tcp'))))
        print(fmt("psutil.net_connections(udp)(客户端仍用)", bench(lambda: psutil.net_connections(kind='udp'))))
        print(fmt("_win_sys_counts(进程/线程/就绪队列)", bench(mod._win_sys_counts)))
        print(fmt("_win_load_step(load EWMA,由 tupd 驱动)", bench(lambda: mod._win_load_step(0))))
        if hasattr(mod, '_win_mem_native'):
            print(fmt("_win_mem_native(内存,客户端实际用)", bench(mod._win_mem_native)))
            print(fmt("_win_mem_gpi(内存对照:GetPerformanceInfo)", bench(mod._win_mem_gpi)))

    print("\n[4] 主循环每轮上报近似（不含 INTERVAL sleep 与网络收发）")
    print(HEAD)
    def report_like():
        mod.get_cpu_cores()
        mod.get_uptime()
        mod.get_memory()
        mod.get_swap()
        mod.get_hdd()
        one_net_round()
        return 0
    print(fmt("cores+uptime+mem+swap+hdd+net", bench(report_like)))

    print("\n[5] psutil 版真实依赖检查")
    checks = [
        ("psutil.boot_time()", lambda: psutil.boot_time()),
        ("psutil.virtual_memory()", lambda: psutil.virtual_memory()),
        ("psutil.swap_memory()", lambda: psutil.swap_memory()),
        ("psutil.disk_partitions()", lambda: psutil.disk_partitions()),
        ("psutil.net_io_counters(pernic=True)", lambda: psutil.net_io_counters(pernic=True)),
        ("psutil.pids()", lambda: psutil.pids()),
        ("psutil.net_connections(tcp)", lambda: psutil.net_connections(kind='tcp')),
        ("shutil.which('powershell')", lambda: __import__('shutil').which('powershell')),
    ]
    if sys.platform.startswith('win') and hasattr(mod, '_win_sys_counts'):
        checks.insert(0, ("_win_sys_counts()", mod._win_sys_counts))
    for name, fn in checks:
        try:
            fn(); print(f"  {name:32s} OK")
        except Exception as e:
            print(f"  {name:32s} FAIL {type(e).__name__}: {str(e)[:60]}")
    print("\n== 完成。把以上输出贴回来即可分析。 ==")

if __name__ == "__main__":
    main()
