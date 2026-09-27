#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tupd 改后对账：tupd()（直读 /proc）与 _tupd_subprocess()（ss/ps）逐次比对 + 成本。"""
import importlib.util
import resource
import sys
import time


def load(path):
    spec = importlib.util.spec_from_file_location("clinux", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cpu(kind):
    r = resource.getrusage(kind)
    return r.ru_utime + r.ru_stime


def bench(fn, n=5):
    c0, w0 = cpu(resource.RUSAGE_SELF), time.perf_counter()
    vals = [fn() for _ in range(n)]
    return ((cpu(resource.RUSAGE_SELF) - c0) / n * 1000,
            (time.perf_counter() - w0) / n * 1000,
            vals[-1])


def child_cpu(fn, n=5):
    c0 = cpu(resource.RUSAGE_CHILDREN)
    for _ in range(n):
        fn()
    return (cpu(resource.RUSAGE_CHILDREN) - c0) / n * 1000


def main():
    mod = load(sys.argv[1] if len(sys.argv) > 1 else "client-linux.py")
    print("== 计数逐次比对（各 5 次，逐次交错，排除时序漂移）")
    print("   %-28s %-28s" % ("ss/ps (tcp,udp,proc,thread)", "/proc (tcp,udp,proc,thread)"))
    ok = 0
    for i in range(5):
        a = mod._tupd_subprocess()
        b = mod.tupd()
        same = all(abs(x - y) <= max(2, abs(y) // 20) for x, y in zip(a, b))
        ok += same
        print("  %d %-28s %-28s %s" % (i, a, b, "OK" if same else "差异"))
    print("   %d/5 在容差内" % ok)

    self_cpu, wall, last = bench(mod.tupd)
    print("\n== 成本")
    print("  tupd() 自身CPU  %7.3f ms/次   墙钟 %7.3f ms/次   -> %s" % (self_cpu, wall, last))
    print("  tupd() 子进程CPU %7.3f ms/次" % child_cpu(mod.tupd))
    print("  legacy 子进程CPU %7.3f ms/次  (tupd 已不走这里)" % child_cpu(mod._tupd_subprocess))


if __name__ == "__main__":
    main()
