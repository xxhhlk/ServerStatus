#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""_disk_io 改前/改后对照：per-cycle CPU + 同窗口上报值比对（在真实 Linux 上运行）。

用法: python3 _verify_disk_io.py <old_client.py> <new_client.py> [-n 20]

[1] per-cycle CPU：把 time.sleep 换成空转计数，跑固定秒数后 CPU 增量 / 轮数。
[2] 同窗口上报值：两版同时跑（各自真实 INTERVAL），主线程每 INTERVAL 采一次 diskIO，
    逐轮并排比对。同一时间窗内两者测的是同一批进程，数值应当接近。
"""
import importlib.util
import os
import resource
import sys
import threading
import time
import types


def load(path):
    spec = importlib.util.spec_from_file_location(
        "cli_" + os.path.basename(path).replace(".", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cpu_now():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


class _Stop(Exception):
    pass


def _guard(fn):
    try:
        fn()
    except _Stop:
        pass


def bench_loop(fn, seconds=5.0):
    """sleep 置空转，跑固定秒数；返回 (CPU ms/轮, 轮数)。"""
    cycles = [0]
    stop = threading.Event()
    real_sleep = time.sleep
    real_time = fn.__globals__["time"]

    def fake_sleep(_seconds):
        if stop.is_set():
            raise _Stop
        cycles[0] += 1

    fn.__globals__["time"] = types.SimpleNamespace(sleep=fake_sleep)
    try:
        c0 = cpu_now()
        t = threading.Thread(target=_guard, args=(fn,), daemon=True)
        t.start()
        real_sleep(seconds)
        c1 = cpu_now()
    finally:
        stop.set()
        fn.__globals__["time"] = real_time
        t.join(timeout=10)
    if cycles[0] == 0:
        return None, 0
    return (c1 - c0) * 1000 / cycles[0], cycles[0]


def sample_lockstep(old, new, samples):
    """两版同时运行（真实 INTERVAL），主线程每 INTERVAL 采一次 diskIO。"""
    stop = threading.Event()
    saved = [old._disk_io.__globals__["time"], new._disk_io.__globals__["time"]]

    def run(mod):
        def sleeper(seconds):
            if stop.is_set():
                raise _Stop
            time.sleep(seconds)
        mod._disk_io.__globals__["time"] = types.SimpleNamespace(sleep=sleeper)
        _guard(mod._disk_io)

    threads = [threading.Thread(target=run, args=(m,), daemon=True) for m in (old, new)]
    for t in threads:
        t.start()
    old_vals, new_vals = [], []
    try:
        for _ in range(samples):
            time.sleep(1.0)
            old_vals.append((old.diskIO["read"], old.diskIO["write"]))
            new_vals.append((new.diskIO["read"], new.diskIO["write"]))
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)
        old._disk_io.__globals__["time"] = saved[0]
        new._disk_io.__globals__["time"] = saved[1]
    return old_vals, new_vals


def main():
    argv = sys.argv[1:]
    samples = 20
    if "-n" in argv:
        i = argv.index("-n")
        samples = int(argv[i + 1])
        del argv[i:i + 2]
    if len(argv) < 2:
        print(__doc__)
        return 1
    old, new = load(argv[0]), load(argv[1])
    nproc = len([d for d in os.listdir("/proc") if d.isdigit()])
    print("进程数=%d  INTERVAL=%s" % (nproc, new.INTERVAL))

    print("\n[1] _disk_io per-cycle CPU（sleep 置空转，5s）")
    print("  %-28s %12s %8s" % ("版本", "CPU ms/轮", "轮数"))
    for label, mod in (("改前(两遍 io+comm)", old), ("改后(单遍 + comm 缓存)", new)):
        ms, cycles = bench_loop(mod._disk_io, seconds=5.0)
        print("  %-28s %12s %8d" % (label, "FAIL" if ms is None else "%.3f" % ms, cycles))

    print("\n[2] 同窗口上报值（同时跑，主线程每 %ss 采一次，共 %d 次）" % (new.INTERVAL, samples))
    ov, nv = sample_lockstep(old, new, samples)
    print("  %4s %12s %12s | %12s %12s" % ("轮", "旧read", "旧write", "新read", "新write"))
    for i, ((or_, ow), (nr, nw)) in enumerate(zip(ov, nv)):
        print("  %4d %12d %12d | %12d %12d" % (i, or_, ow, nr, nw))
    print("  合计 旧 read=%d write=%d | 新 read=%d write=%d" % (
        sum(r for r, _ in ov), sum(w for _, w in ov),
        sum(r for r, _ in nv), sum(w for _, w in nv)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
