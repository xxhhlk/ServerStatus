#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Linux 客户端缓存改动对照：get_os_name / _is_physical_interface / liuliang。

用法: python3 _verify_linux_cache.py <old_client.py> <new_client.py>

[1] per-call CPU：三个函数改前/改后（新实现第二次起命中缓存）。
[2] 等价性：get_os_name 返回值、每个真实网卡的物理性判定、liuliang 的累计流量
    必须一致（缓存不得改变分类结果）。
"""
import importlib.util
import os
import resource
import sys
import time


def load(path):
    spec = importlib.util.spec_from_file_location(
        "cli_" + os.path.basename(path).replace(".", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cpu_now():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def bench(fn, seconds=1.5):
    """返回 (CPU ms/次, 次数)。先热身一次，避免把首次缓存填充算进稳态。"""
    fn()
    c0 = cpu_now()
    t0 = time.perf_counter()
    n = 0
    while time.perf_counter() - t0 < seconds:
        fn()
        n += 1
    return (cpu_now() - c0) * 1000 / n, n


def iface_names():
    names = []
    with open('/proc/net/dev') as f:
        for line in f:
            if ':' in line:
                names.append(line.split(':')[0].strip())
    return names


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    old, new = load(sys.argv[1]), load(sys.argv[2])
    names = iface_names()
    print("网卡 %d 个: %s" % (len(names), ", ".join(names)))

    print("\n[1] per-call CPU（稳态，各跑 1.5s）")
    print("  %-34s %14s %14s %10s" % ("函数", "改前 ms", "改后 ms", "倍率"))
    cases = [
        ("get_os_name()", lambda: old.get_os_name(), lambda: new.get_os_name()),
        ("liuliang()", old.liuliang, new.liuliang),
        ("_is_physical_interface(eth0 名)", lambda: old._is_physical_interface(names[0]),
         lambda: new._is_physical_interface(names[0])),
    ]
    for label, fo, fn in cases:
        a, _ = bench(fo)
        b, _ = bench(fn)
        print("  %-34s %14.5f %14.5f %9.1fx" % (label, a, b, a / b if b else 0))

    print("\n[2] 等价性")
    print("  get_os_name: 改前=%r 改后=%r %s"
          % (old.get_os_name(), new.get_os_name(),
             "OK" if old.get_os_name() == new.get_os_name() else "差异"))
    bad = [n for n in names
           if old._is_physical_interface(n) != new._is_physical_interface(n)]
    print("  物理性判定: %d/%d 一致 %s"
          % (len(names) - len(bad), len(names), "OK" if not bad else "差异 %s" % bad))
    print("  物理网卡: %s" % ", ".join(n for n in names if new._is_physical_interface(n)))
    a, b = old.liuliang(), new.liuliang()
    print("  liuliang: 改前=%s 改后=%s（累计量，两次读数之间会增长）" % (a, b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
