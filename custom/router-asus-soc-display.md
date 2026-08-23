# 华硕路由器芯片型号显示（cpu_model）

## 背景

华硕博通路由器上，`/proc/cpuinfo` 与 `lscpu` 只能读到核心名（如 `Brahma-B53`），
无法直接读取处理器商品型号（如 BCM4916）。而 `nvram get productid` 可以读到
路由器商品型号（如 `RT-BE88U`）。

本改动在客户端内置华硕路由器「型号 → SoC」映射表（源自 `asus_routers_spec_soc_full.xlsx`，
131 款华硕路由器），自动读取 nvram productid 并显示芯片型号。

## 实现（custom/client-linux-router.py）

- `ASUS_SOC_MAP`：147 条映射。
  - 键：nvram productid 规范化（大写 + 去所有非字母数字），如 `RT-BE88U -> RTBE88U`。
  - 值：仅芯片名（统一风格），多芯片用 `+` 连接，如 `BCM6750+BCM6715`。
- `_get_nvram_productid()`：执行 `nvram get productid`，2 秒超时，失败静默返回 None。
- `get_cpu_model_display()`：cpu_model 三态显示策略：
  1. productid 命中映射表 → 返回芯片型号（如 `BCM4916`）
  2. productid 未收录 → 返回 `RT-XXX (Brahma-B53)`（型号 + 原逻辑结果）
  3. 非华硕 / 读不到 nvram → 返回原 `get_cpu_model()` 结果
- 主循环调用点 `CPUModel = get_cpu_model()` 改为 `get_cpu_model_display()`（每连接计算一次）。

## 映射表生成（custom/gen_asus_soc_map.py）

从 `asus_routers_spec_soc_full.xlsx` 重新生成映射表的脚本，更新表格后运行：

```bash
python3 custom/gen_asus_soc_map.py
```

生成规则与人工裁决：

- **完整 token 提取**：后缀不拆开（`RT-AX86U Pro -> RTAX86UPRO`、`RT-BE58U V2 -> RTBE58UV2`），
  避免裸码被 Pro/V2 行污染（如 `RT-AX86U`=BCM4908，`RT-AX86U_PRO`=BCM4912）。
- **行首主码优先**：同一产品码出现在多行时，取作为行首主码的那条（如
  `RT-AX1800`=MT7621AT 来自主行，而非别名行 `RT-AX55（…RT-AX1800 Plus）`=BCM6755）。
- **SoC 清洗**：去括号注释（含芯片码的括号保留，如 `(BCM4708A0)`）、剔除 `xx` 系列名、
  合并前缀包含的冗余码（`BCM4708 (BCM4708A0)` -> `BCM4708A0`）。
- **人工 override**（脚本内 `OVERRIDE`）：
  - `RTBE3600`/`TUFBE3600` = `BCM6764L`（RT-BE3600 初版对应 BCM6764L，表中 V2 行括号内
    出现导致先到先得错配）
  - `TUFAX5400`/`TUFAX6000`/`TUFAX3000`/`TXAX6000` 补充官方产品码键
  - `BLUECAVE`/`LYRAVOICE` 无连字符码，补充整行产品名键
  - `BE14000`/`BE30000`/`BE3600`/`BE5000` 纯速率命名型号（nvram productid 即速率名）

## 验证

- `python3 -m py_compile custom/client-linux-router.py` 通过。
- mock 测试 4 分支：命中表 / 未收录 / 无 nvram / 下划线型号（`RT-AX86U_PRO`），全部通过。
- 歧义条目回归：`RT-AX86U`=BCM4908 vs `RT-AX86U_PRO`=BCM4912、
  `RT-BE58U`=BCM6764L vs `RT-BE58U_V2`=BCM6764、
  `RT-AC1900`=BCM4708A0、`RT-AC87U`=BCM4709A0 等全部正确。

## 部署

路由器上重启客户端即可，WebUI 的 cpu_model 将显示芯片型号：

```bash
python3 client-linux-router.py
```
