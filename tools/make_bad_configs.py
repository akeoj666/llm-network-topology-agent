"""从「好配置」生成「坏配置」样本（C3 的输入）。

为什么要程序生成而不是手写：手写的坏配置容易自己就不合法（语法都错），
测出来的只是「解析器健不健壮」，不是「能不能定位真故障」。
这里从 `config.generate_all()` 的真实产物里只改**一处**语义错误，
其余字节与能跑通的配置完全一致——这正是「教师预置一个故障」的样子。

    python tools/make_bad_configs.py            # 生成到 bad_configs/
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import config  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

OUT = ROOT / "bad_configs"


def good(uid: str) -> dict[str, list[str]]:
    """uid 是 UC-01 这种编号，文件名是小写 uc01.yaml。"""
    return config.generate_all(Topology.load(ROOT / "examples" / f"{uid.replace('-', '').lower()}.yaml"))


def drop_line(cfgs: dict[str, list[str]], dev: str, needle: str) -> None:
    lines = cfgs[dev]
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == needle)
    lines.pop(idx)


def sub_line(cfgs: dict[str, list[str]], dev: str, old: str, new: str) -> None:
    lines = cfgs[dev]
    idx = next(i for i, ln in enumerate(lines) if ln.strip() == old)
    indent = lines[idx][: len(lines[idx]) - len(lines[idx].lstrip(" "))]
    lines[idx] = f"{indent}{new}"


CASES: dict[str, dict] = {
    "case1_漏network": {
        "from": "UC-01",
        "坏在哪": "R1 的 router rip 少宣告了 10.0.0.0，R2 学不到行政部网段",
        "mutate": lambda c: drop_line(c, "R1", "network 10.0.0.0"),
    },
    "case2_错掩码": {
        "from": "UC-01",
        "坏在哪": "R1 与 R2 之间的 /30 互联口被写成 255.255.255.0",
        "mutate": lambda c: sub_line(c, "R1", "ip address 10.0.0.1 255.255.255.252",
                                     "ip address 10.0.0.1 255.255.255.0"),
    },
    "case3_VLAN未建": {
        "from": "UC-05",
        "坏在哪": "SW0 把端口划进 VLAN 20，但 VLAN 数据库里没有 vlan 20（PT 里 VLAN 20 不出现）",
        "mutate": lambda c: (drop_line(c, "SW0", "vlan 20"), drop_line(c, "SW0", "name MKT")),
    },
    "case4_ACL未定义": {
        "from": "UC-07",
        "坏在哪": "R1 的客人子接口挂了 access-group 100，但 ACL 100 的条目全被删了（等于全放行）",
        "mutate": lambda c: (drop_line(c, "R1", "access-list 100 deny ip 192.168.30.0 0.0.0.255 192.168.40.0 0.0.0.255"),
                             drop_line(c, "R1", "access-list 100 permit ip any any")),
    },
    "case5_地址非法": {
        "from": "UC-01",
        "坏在哪": "R3 的业务口写成网段号 192.168.16.128/26（该网段的网络地址）",
        "mutate": lambda c: sub_line(c, "R3", "ip address 192.168.16.190 255.255.255.192",
                                     "ip address 192.168.16.128 255.255.255.192"),
    },
}


def main() -> int:
    OUT.mkdir(exist_ok=True)
    index = ["# C3 的坏配置样本", "",
             "全部由 `tools/make_bad_configs.py` 从真实生成物里**只改一处**得到，",
             "其余字节与能跑通的配置一致。", "",
             "| 样本 | 来源用例 | 坏在哪 |", "|---|---|---|"]
    for name, meta in CASES.items():
        cfgs = good(meta["from"])
        meta["mutate"](cfgs)
        d = OUT / name
        d.mkdir(exist_ok=True)
        for dev, lines in cfgs.items():
            (d / f"{dev}.cfg").write_text("\n".join(lines) + "\n", encoding="utf-8")
        index.append(f"| `{name}` | {meta['from']} | {meta['坏在哪']} |")
        print(f"{name}: {len(cfgs)} 台设备 → {d}")
    (OUT / "README.md").write_text("\n".join(index) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
