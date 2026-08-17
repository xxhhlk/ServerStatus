#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
client-psutil.py 耗时诊断脚本（在真实环境运行，不连接服务器）
用法: python _profile_psutil_standalone.py [client-psutil.py 路径]
      默认找当前目录 client-psutil.py；不填路径时也尝试 OneDrive 副本位置。
输出: 各热点函数单轮平均耗时 + Windows 兼容性检查
"""
import importlib.util, time, timeit, sys, os, platform

def load_mod(target):
    spec = importlib.util.spec_from_file_location("cps", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def bench(fn, rounds=3, warmup=True):
    if warmup:
        try: fn()
        except Exception: pass
    ts = []
    for _ in range(rounds):
        t0 = timeit.default_timer()
        try:
            fn()
            ts.append(timeit.default_timer() - t0)
        except Exception as e:
            return None, type(e).__name__ + ": " + str(e)[:80]
    return sum(ts)/len(ts), None

def fmt(name, r):
    if r[1]: return f"{name:32s} FAIL    {r[1]}"
    return f"{name:32s} {r[0]*1000:9.2f} ms"

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else "client-psutil.py"
    if not os.path.exists(target):
        alt = os.path.expandvars(r"%USERPROFILE%\OneDrive\文档\client-psutil.py")
        if os.path.exists(alt): target = alt
        else:
            print(f"找不到文件: {target}"); sys.exit(1)
    print(f"== 环境: {platform.platform()} | python {platform.python_version()} | "
          f"psutil {__import__('psutil').__version__}")
    print(f"== 分析目标: {os.path.abspath(target)}")
    mod = load_mod(target)

    print("\n[1] 系统信息类耗时（每轮上报都会调用）")
    print(fmt("get_uptime(psutil.boot_time)", bench(mod.get_uptime, 3)))
    print(fmt("get_memory(psutil.virtual_memory)", bench(mod.get_memory, 3)))
    print(fmt("get_swap(psutil.swap_memory)", bench(mod.get_swap, 3)))
    print(fmt("get_hdd(psutil.disk_partitions)", bench(mod.get_hdd, 3)))
    print(fmt("get_cpu_model", bench(mod.get_cpu_model, 3)))
    print(fmt("os.getloadavg(仅linux/darwin用)", bench(lambda: os.getloadavg(), 3)))

    print("\n[2] 网络热点")
    print(fmt("_win_build_map 首次(PowerShell)", bench(mod._win_build_map, 2, warmup=False)))
    print(fmt("_win_build_map 缓存命中", bench(mod._win_build_map, 3, warmup=False)))
    mod._win_cache_clock = 0
    import psutil
    try:
        nics = list(psutil.net_io_counters(pernic=True).keys())
    except Exception as e:
        nics = []
    print(f"网卡列表: {nics}")
    for n in nics:
        print(fmt(f"is_virtual_nic({n!r})", bench(lambda n=n: mod.is_virtual_nic(n), 5, warmup=False)))
    print(fmt("psutil.net_io_counters(pernic=True) 裸调", bench(lambda: psutil.net_io_counters(pernic=True), 5)))
    def one_net_round():
        net = psutil.net_io_counters(pernic=True)
        ti = to = 0
        for k, v in net.items():
            if mod.is_virtual_nic(k): continue
            ti += v[1]; to += v[0]
        return ti, to
    print(fmt("完整一轮采样(计数+虚拟过滤)", bench(one_net_round, 5)))
    print(fmt("get_network(连接80端口)", bench(lambda: mod.get_network(4), 2)))

    print("\n[3] 子进程/原生调用类")
    print(fmt("tupd(平台分支: win原生/linux ss)", bench(mod.tupd, 2, warmup=False)))
    if hasattr(mod, '_win_proc_thread_count') and sys.platform.startswith('win'):
        print(fmt("_win_proc_thread_count(NtQuerySystemInformation)", bench(mod._win_proc_thread_count, 3, warmup=False)))
        print(fmt("psutil.net_connections(tcp)", bench(lambda: psutil.net_connections(kind='tcp'), 2, warmup=False)))
        print(fmt("psutil.net_connections(udp)", bench(lambda: psutil.net_connections(kind='udp'), 2, warmup=False)))

    print("\n[4] 主循环每轮上报近似耗时（不含 INTERVAL sleep 与网络收发）")
    def report_like():
        mod.get_cpu_cores()
        mod.get_uptime()
        mod.get_memory()
        mod.get_swap()
        mod.get_hdd()
        one_net_round()
        return 0
    print(fmt("cores+uptime+mem+swap+hdd+net", bench(report_like, 5)))

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
    if sys.platform.startswith('win') and hasattr(mod, '_win_proc_thread_count'):
        checks.insert(0, ("_win_proc_thread_count()", mod._win_proc_thread_count))
    for name, fn in checks:
        try:
            fn(); print(f"  {name:32s} OK")
        except Exception as e:
            print(f"  {name:32s} FAIL {type(e).__name__}: {str(e)[:60]}")
    print("\n== 完成。把以上输出贴回来即可分析。 ==")

if __name__ == "__main__":
    main()
