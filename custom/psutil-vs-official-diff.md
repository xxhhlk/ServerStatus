# client-psutil.py 自定义版 vs 官方 master 版代码级对比

| | 官方 master 版 | 自定义版（工作区 = psutil-net-monitor） |
|---|---|---|
| 版本 | 1.1.0（2025-09-02） | 1.1.0 基线 + PNPDeviceID 定制 |
| 网络模型 | **双线程 + 互斥锁** | **单线程 + 全局快照** |
| 虚拟网卡识别 | 名字黑名单 | Windows PNPDeviceID / Linux /sys 软链接 |
| 差异规模 | — | +~100 行（虚拟网卡识别 + 单线程监控），-2 个函数 |

---

## 一、核心架构差异：锁方案 vs 单线程

### 官方版（`_get_net_io_counters()` + 锁）
```python
_net_io_counters_lock = threading.Lock()

def _get_net_io_counters():
    with _net_io_counters_lock:
        return psutil.net_io_counters(pernic=True)
```
- **两个调用方**：`liuliang()`（主循环，每秒）+ `_net_speed()`（独立线程，每秒）
- 用 `threading.Lock()` 保证 `psutil.net_io_counters()` 不被并发调用（官方 844a5dd 修 Windows 流量虚高的方案）
- 锁只能保证互斥，**两个线程仍各自做全量快照**，每个周期读两次

### 自定义版（`_net_monitor()` 单线程 + 全局快照）
```python
_net_in, _net_out = 0, 0          # 全局累计值
_net_lock = threading.Lock()      # 保护全局快照（读写都很短）

def _net_monitor():               # 独立线程：唯一调 psutil 的地方
    while True:
        net = psutil.net_io_counters(pernic=True)
        total_in = sum(v[1] for k, v in net.items() if not is_virtual_nic(k))
        total_out = sum(v[0] for k, v in net.items() if not is_virtual_nic(k))
        with _net_lock:
            _net_in, _net_out = total_in, total_out
        # 同一次快照算 netrx/nettx
        ...
        time.sleep(INTERVAL)

def liuliang():                    # 主循环只读全局值，不调 psutil
    with _net_lock:
        return _net_in, _net_out
```
- **psutil 只有 `_net_monitor` 一个线程调用**——并发问题从根源消除，锁仅保护极短的全局变量读写
- 累计值 + 速率**同一次快照计算**，两个指标天然一致（官方版两个线程各采一次，速率与累计值可能差一拍）

## 二、虚拟网卡识别：名字黑名单 vs 物理性判定

### 官方版（纯名字黑名单，Windows/Linux 通用）
```python
if 'lo' in k or 'tun' in k or 'docker' in k or 'veth' in k \
        or 'br-' in k or 'vmbr' in k or 'vnet' in k or 'kube' in k:
    continue
```
- 覆盖 Linux 容器/虚拟接口，但对 Windows 无效（Windows 网卡名不含这些关键字）
- Windows 上会把 **vEthernet、Wi-Fi Direct、Bluetooth、TAP、Tailscale/WireGuard 虚拟网卡**的流量计入

### 自定义版（`is_virtual_nic()`，平台感知）
```python
_WIN_NAME_HINTS = ('Loopback', 'Wi-Fi Direct', 'WAN Miniport', 'Bluetooth',
                   'Apple Mobile', 'Kernel Debug', 'TAP-', 'OpenVPN',
                   'Tailscale', 'WireGuard', 'VMware', 'VirtualBox',
                   'Host-Only', 'vEthernet', 'Default Switch')

def _win_is_physical(pnp_id):
    return bool(pnp_id) and pnp_id.startswith(('PCI\\', 'USB\\'))

def _win_build_map():   # PowerShell Get-CimInstance，10 分钟缓存
    ...

def is_virtual_nic(name):
    if sys.platform.startswith('win'):
        # ① PNPDeviceID 映射：PCI\ / USB\ = 物理；ROOT\ / SWD\ / BTH\ / {GUID} = 虚拟
        # ② 名字提示词黑名单（兜底）
        # ③ psutil.net_if_stats()[name].speed == 0 → 虚拟（最后兜底）
    if name == 'lo':
        return True
    if not os.access('/sys/class/net', os.R_OK):
        return False   # Android 非 root 无 /sys → 宁可计入不崩
    return not os.path.exists('/sys/class/net/%s/device' % name)  # Linux: 无 device 软链接=非物理
```
- **Windows 三层判定**：PNPDeviceID（主）→ 名字提示（兜底）→ speed==0（最后兜底）
- VM 内主网卡（VEN_80EE=VMware / 1AF4=QEMU / 15AD=VMware）也是 `PCI\` 开头，**不会被误伤**
- `_win_build_map()` 每 10 分钟才调一次 PowerShell（缓存 `_win_cache_clock`），失败保留旧值——不拖慢每轮上报
- Linux 改用 `/sys/class/net/<if>/device` 软链接存在性判断，比名字黑名单更准（docker0/veth/tun/br- 都没有 device 链接）；Android 无 /sys 权限时宁可多统计

## 三、函数级差异表

| 函数 | 官方版 | 自定义版 |
|---|---|---|
| `_get_net_io_counters()` | ✅ 存在（锁封装） | ❌ 删除 |
| `_net_io_counters_lock` | ✅ 存在 | ❌ 删除 |
| `_net_speed()` | ✅ 独立线程+锁调 psutil | ❌ 删除（被 `_net_monitor` 取代） |
| `_net_monitor()` | ❌ | ✅ 唯一调 psutil 的线程，存全局快照+算速率 |
| `liuliang()` | 每轮调 psutil 累加（黑名单过滤） | 只读全局快照（过滤已在 _net_monitor 完成） |
| `is_virtual_nic()` / `_win_is_physical()` / `_win_build_map()` | ❌ | ✅ 新增（PNPDeviceID 虚拟网卡识别） |
| `get_realtime_data()` t4 | `target=_net_speed` | `target=_net_monitor` |
| 其余全部（get_uptime/memory/swap/hdd/cpu/cpu_model/tupd/_ping_thread/_disk_io/_monitor_thread/byte_str/main/上报字段） | — | **逐行一致** |

## 四、结论

- 自定义版是**官方 1.1.0 + 单线程网络监控重构**，不是功能增减，而是架构改进：
  1. **单线程化**：psutil 并发读从根源消除，比官方锁方案更彻底（官方锁下两线程仍各采一次全量快照）
  2. **PNPDeviceID 物理性判定**：解决 Windows 虚拟网卡（vEthernet/Tailscale/WireGuard/TAP/蓝牙等）流量误计入，这是官方黑名单方案在 Windows 上的盲区
  3. **Linux /sys 判定**：比名字黑名单更准确
- 代价：Windows 上依赖 PowerShell（每 10 分钟一次，失败降级）+ 引入 `subprocess` 依赖；无 PowerShell 的环境靠名字提示/speed 兜底
- 保持一致的：上报协议、监控探针（latency-only）、cpu_model 降级链、磁盘 IO、全部配置/env/CLI 解析
