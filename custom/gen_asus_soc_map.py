# -*- coding: utf-8 -*-
"""一次性工具：从 asus_routers_spec_soc_full.xlsx 生成 ASUS_SOC_MAP 映射表。"""
import zipfile
import re
import xml.etree.ElementTree as ET

XLSX = 'asus_routers_spec_soc_full.xlsx'
OUT = '_asus_soc_map.py'


def read_rows():
    z = zipfile.ZipFile(XLSX)
    root = ET.fromstring(z.read('xl/worksheets/sheet1.xml').decode('utf-8'))
    ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    rows = []
    for row in root.findall('.//m:row', ns):
        cells = []
        for c in row.findall('m:c', ns):
            v = c.find('m:v', ns)
            is_ = c.find('m:is', ns)
            val = ''
            if v is not None and v.text:
                val = v.text
            elif is_ is not None:
                t = is_.find('m:t', ns)
                if t is not None and t.text:
                    val = t.text
            cells.append(val)
        rows.append(cells)
    return rows


def norm(s):
    """规范化键：大写 + 去所有非字母数字（RT-BE88U -> RTBE88U）"""
    return re.sub(r'[^a-zA-Z0-9]', '', s).upper()


# 无连字符纯码白名单前缀（GS7 / GT6 / XD6 / BT10 / BQ16 / CT8 ...）
BARE_PREFIX = ('GS', 'GT', 'XD', 'XT', 'ET', 'BD', 'BQ', 'BT', 'CT', 'XC')


def _suffix(model, start):
    """合并后续大写后缀词（Pro/Go/V2/B1/AI/Plus/Outdoor...），需为完整词，避免吞入中文"""
    sm = re.match(r' +[A-Z][A-Za-z0-9]*(?: +[A-Z][A-Za-z0-9]*)*', model[start:])
    if sm:
        return sm.group(0).strip()
    return ''


def extract_tokens(model):
    """从型号列提取完整产品码 token（后缀不拆开：RT-AX86U Pro / RT-BE58U V2 / GT-BE96 AI）"""
    tokens = []
    # 模式1: 带连字符的码，可后接空格+大写后缀词
    for m in re.finditer(r'[A-Za-z]{2,4}(?:-[A-Za-z0-9]+)+', model):
        tok = m.group(0)
        suf = _suffix(model, m.end())
        if suf:
            tok += ' ' + suf
        tokens.append(tok)
    # 模式2: 无连字符纯码白名单  GS7 / XD4S / BT10 / XD4 Plus
    for m in re.finditer(r'(?<![A-Za-z])(?:GS|GT|XD|XT|ET|BD|BQ|BT|CT|XC)\d{1,3}[A-Za-z]?', model):
        tok = m.group(0)
        suf = _suffix(model, m.end())
        if suf:
            tok += ' ' + suf
        tokens.append(tok)
    return tokens


def clean_soc(soc):
    """清洗 SoC 列：去括号注释（全角/半角/方括号），提取芯片码，冗余包含项合并，+ 连接"""
    # 括号内含芯片码（如 (BCM4708A0) 步进信息）时保留，其余注释括号删除
    s = re.sub(r'[（(](?!\s*(?:BCM|MT|IPQ|QCA|QCN|GRX)\d)[^（()）]*[）)]', ' ', soc)
    s = re.sub(r'\[[^\]]*\]', ' ', s)
    codes = re.findall(
        r'BCM\d+[A-Za-z]*\d*|MT\d{4,}[A-Za-z]*\d*|IPQ\d+[A-Za-z]*\d*|QCA\d+[A-Za-z]*\d*|'
        r'QCN\d+[A-Za-z]*\d*|GRX\d+[A-Za-z0-9]*|Filogic \d+',
        s, re.IGNORECASE)
    # 去重保序，剔除系列名（IPQ40xx 这类含 xx 的非具体芯片）
    uniq = []
    for c in codes:
        if c not in uniq and 'xx' not in c.lower():
            uniq.append(c)
    # 去掉被其他码前缀包含的冗余项（BCM4708 被 BCM4708A0 包含 -> 留 BCM4708A0）
    final = [c for c in uniq if not any(c != o and o.startswith(c) for o in uniq)]
    return '+'.join(final)


# 人工修正：裸码歧义（V1/V2 同码）与补充键
OVERRIDE = {
    'RTBE3600': 'BCM6764L',      # RT-BE3600 初版对应 BCM6764L（表中 V2 行括号内出现导致先到先得错误）
    'TUFBE3600': 'BCM6764L',     # TUF-BE3600 (V1) 补充裸码键
    'TUFAX5400': 'BCM6750',      # TUF Gaming AX5400 的官方产品码补充
    'TUFAX6000': 'MT7986AV',     # TUF Gaming AX6000 的官方产品码补充
    'TUFAX3000': 'BCM6750',      # TUF 小旋风 WiFi6 AX3000 -> TUF-AX3000 (V1)
    'TXAX6000': 'MT7986A',       # TX GAMING AX6000 天选游戏路由 -> TX-AX6000
    'LYRAVOICE': 'IPQ4019',      # Lyra Voice 无连字符码
    'BLUECAVE': 'GRX350',        # Blue Cave 无连字符码，整行产品名
    'BE14000': 'MT7988DV',       # ZenWiFi BE14000：纯速率命名，nvram productid 即速率名
    'BE30000': 'BCM4916',        # ZenWiFi BE30000
    'BE3600': 'IPQ5322',         # ZenWiFi BE3600
    'BE5000': 'IPQ5322',         # ZenWiFi BE5000
}
# 上述补充键按 norm 化后的顺序覆盖映射表结果
OVERRIDE_NORM = {norm(k): v for k, v in OVERRIDE.items()}


def main():
    rows = read_rows()
    mapping = {}
    skipped_no_token = []
    skipped_no_soc = []
    for r in rows[1:]:
        if len(r) < 9:
            continue
        model, soc = r[1].strip(), r[8].strip()
        tokens = extract_tokens(model)
        cleaned = clean_soc(soc)
        if not tokens:
            skipped_no_token.append((model, soc))
            continue
        if not cleaned:
            skipped_no_soc.append((model, soc))
            continue
        # 行首主码优先：后续别名仅当键不存在时补
        for i, t in enumerate(tokens):
            key = norm(t)
            if key not in mapping:
                mapping[key] = cleaned

    for key, val in OVERRIDE_NORM.items():
        mapping[key] = val

    print(f'共 {len(mapping)} 条映射')
    print('\n== 无产品码被跳过的行:')
    for m, s in skipped_no_token:
        print(f'  {m!r} -> {s!r}')
    print('\n== 芯片码清洗失败的行:')
    for m, s in skipped_no_soc:
        print(f'  {m!r} -> {s!r}')

    with open(OUT, 'w', encoding='utf-8') as f:
        f.write('# -*- coding: utf-8 -*-\n')
        f.write('ASUS_SOC_MAP = {\n')
        for k in sorted(mapping):
            f.write(f'    {k!r}: {mapping[k]!r},\n')
        f.write('}\n')
    print(f'\n已写出 {OUT}')


if __name__ == '__main__':
    main()
