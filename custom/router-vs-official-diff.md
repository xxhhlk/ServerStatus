# client-linux-router.py vs 官方 client-linux.py 代码级对比

| | custom/client-linux-router.py | clients/client-linux.py（官方 master） |
|---|---|---|
| 版本 | 1.0.3（基于 2022-05-30 上游） | 1.1.0（2025-09-02 官方最新） |
| Python | 2.7 ~ 3.10 兼容 | 3.6+（移除 Py2 兼容） |
| 定位 | 华硕 Merlin 路由器定制 | 通用服务器/Docker |

---

## 一、头部配置差异

| 配置项 | router 版 | 官方版 |
|---|---|---|
| SERVER / USER | 已填（xxhhlk 私有） | 空，待填 |
| PORT | 37014 | 35601 |
| CU/CT/CM | zstaticcdn.com 三网探针 | cloudcpp.com 三网探针 |
| PROBEPORT | 443 | 80 |
| PROBE_PROTOCOL_PREFER | ipv6 | ipv4 |
| PING_PACKET_HISTORY_LEN | 200 | 100 |
| ONLINE_PACKET_HISTORY_LEN | 200 | 已删除（无此概念） |
| socket 默认超时 | 15s | 30s |

## 二、新增机制（官方版有，router 版无）

1. **Docker env 覆盖**：`_env_str()` / `_env_int()`，支持 SERVER/USER/PASSWORD/PORT/INTERVAL/PROBEPORT/PROBE_PROTOCOL_PREFER/PING_PACKET_HISTORY_LEN/CU/CT/CM 环境变量注入。优先级：CLI 参数 > 用户改的常量 > env。
2. **`parse_cli_args()`**：用 `partition('=')` + 5 键白名单解析。router 版是遍历整个 `sys.argv`、只要含子串 `'SERVER'` 就 split —— 误匹配风险高（任何含该子串的参数都会命中），官方版更严谨。
3. **`get_cpu_cores()`**：读 `/proc/stat` 数 `cpu\d+` 行，失败回退 `os.cpu_count()`。
4. **`get_cpu_model()`**：降级链 `/proc/cpuinfo model name → lscpu model name → hardware → processor → platform.processor() → vendor_id → architecture`，过滤泛型型号（x86_64/armv* 等），截断 160 字符。
5. **上报 `os` 字段**：读 `/etc/os-release` 的 ID（如 debian/ubuntu），失败回退 platform 探测。

## 三、函数级差异

### get_hdd() —— 最大差异①
- **router 版：假数据**。`df -Tlm` 命令被注释掉，硬编码 `total=100, used=50` 返回。
- 官方版：遍历 `/proc/mounts` 白名单文件系统（ext4/ext3/ext2/reiserfs/jfs/btrfs/fuseblk/zfs/simfs/ntfs/fat32/exfat/xfs），按设备去重后 `os.statvfs()` 累加，返回 MB。

### liuliang() —— 最大差异②（核心定制点）
- **router 版**：只统计 **`wan0`** 接口（路由器 WAN 口，避免把内网/LAN 流量算进去）。
  - **带持久化状态文件** `/tmp/mnt/router-usb/entware/serverstatus/.net_counter`，记录 `total_in total_out last_raw_in last_raw_out uptime`。
  - 专门处理**光猫/路由器重启归零**：`uptime >= prev_uptime` 判断是否重启，重启则"封口"上一段累计值再重新累计（`NET_IN < last_raw_in` 时用 `last_raw_in` 封口）。
- 官方版：排除虚拟接口（lo/tun/docker/veth/br-/vmbr/vnet/kube + rx/tx 为 0 的），其余全部累加。无持久化、无归零保护。

### tupd()
- router 版：`ss -t`/`ss -u` 计 TCP/UDP，但进程和线程都用 `ps w|wc -l` —— **两处相同命令，thread 实际是进程数（Bug）**。
- 官方版：`ps -ef` 计进程、`ps -eLf` 计线程（已修正）。

### get_network()
- router 版：探测 `ipv4.ip.sb` / `ipv6.ip.sb`。
- 官方版：探测 `ipv4.google.com` / `ipv6.google.com`（国内网络可能不可达，router 版反而更适合大陆）。

### _ping_thread()（丢包/延迟探测）
| 项 | router 版 | 官方版 |
|---|---|---|
| DNS 解析 | 每 150 轮解析一次（省 DNS 查询），失败用 cached_ip 回退 | 每轮都解析（flush dns every time），失败保持原值 |
| 建连超时 | 2s | 1s |
| 历史队列长度 | 200 | 100 |

### _net_speed()
- router 版：只统计 **eth0**（路由器上通常只有 WAN 走 eth0）。
- 官方版：排除 lo/tun/docker/veth/br-/vmbr/vnet/kube 虚拟接口后全统计。

### _monitor_thread() —— 最大差异③（监控探针重写）
router 版（旧协议）：
- 保留 `dns_time / connect_time / download_time` 三段分段计时。
- http/https 真正发 `GET /` 请求并校验状态码（200/204/301/302/401），tcp 发 GET 后 recv。
- 统计 `online_rate` 在线率（丢包队列 `ONLINE_PACKET_HISTORY_LEN=200`，qsize>5 才计算）。
- 存到 monitorServer：`{type, dns_time, connect_time, download_time, online_rate}`。

官方版（新协议，大幅简化）：
- **只测 TCP 建连延迟 `latency`**（timeout=1s），不发任何 HTTP 请求。
- **支持端口解析**：http/https 可带自定义端口（`host:port`，`[::1]:443` IPv6 方括号格式），tcp 必须带端口。
- 未知类型 `sleep` 跳过。ECONNREFUSED 计延迟，其余异常 latency=0。
- 存到 monitorServer：`{type, host, latency}`。
- 注意：官方版**已无在线率概念**，online_rate 相关字段全部移除。

### 主循环上报字段
| 字段 | router 版 | 官方版 |
|---|---|---|
| cpu_cores / cpu_model / os | ❌ 不发送 | ✅ 新增 |
| io_read / io_write | 硬编码 `0`（`_disk_io()` 线程算了但没上报，Bug） | `diskIO.get("read"/"write")` 真实上报 |
| custom 格式 | HTML：`<br>` 拼接 `dns/连接/下载/在线率` | `;` 分隔 `key=ms`，按 key 排序稳定输出 |

### 异常处理
- router 版：打印 `traceback.format_exc()` 全堆栈（便于路由器上排障）。
- 官方版：只打印 `"Disconnected..."`，静默重连。

---

## 四、router 版独有（官方无）

1. **流量归零保护**（光猫/路由器重启后流量计数器不归零，历史累计保留）—— 官方版无此机制，服务器场景不需要。
2. **只统计 wan0/eth0** —— 路由器多网卡场景必须，否则会把 LAN 内网流量计入。
3. Python 2 兼容 Queue 导入。
4. 探针域名/探测口针对大陆网络优化（zstaticcdn、443、ipv6 优先）。

## 五、官方版独有（router 无）

1. Docker env 注入 + 严谨 CLI 解析（多了一层 `os.getenv` 兜底）。
2. `cpu_cores` / `cpu_model` / `os` 上报（新版 web 前端展示 CPU 型号依赖此字段）。
3. 真实磁盘统计（statvfs 多文件系统）。
4. 新版监控探针（latency-only 协议）。
5. 线程计数修正（ps -eLf）。

---

## 六、结论

- router 版 = 官方 2022-05-30 旧版 + **路由器适配层**（wan0 流量 + 归零持久化 + 大陆探针），适用于华硕 Merlin 等路由器场景。
- 官方版 1.1.0 在**通用性、健壮性、信息上报**上全面领先，但**不适合直接跑在路由器上**（流量会把 LAN 计入、无归零保护、探针域名大陆不可达、无 hdd 假数据规避）。
- 若 router 版要同步官方新特性，建议迁移点：① 采纳官方 `get_cpu_cores/get_cpu_model/os` 上报；② 修 `tupd()` 线程计数 bug；③ 修 `io_read/io_write` 硬编码 0；④ 保留 wan0 流量逻辑与归零状态机，其余（_net_speed 接口过滤、get_hdd 真实统计）可按路由器实际接口调整后合并。
