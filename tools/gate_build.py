"""D2 闸门测试：生成一组递进的 .pkt，用来逐层定位 Packet Tracer 的接受度。

gate0  原样重编码            → 编码器写出的文件 PT 认不认
gate1  只改设备名            → 改 NAME 是否安全
gate2  改名 + 注入配置 + 改PC → RUNNINGCONFIG / 主机参数注入是否生效
gate3  删设备                → 骨架裁剪能力
gate4  在原配置上打补丁      → 最保守的配置注入（命令集合与原文件完全一致）
gate5  整段替换为全新配置    → 激进注入（接口名已按真实硬件修正）

用法：python -m tools.gate_build
"""

from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pktlab.codec import read_pt_file
from pktlab.pkt import Pkt

SEED = Path("seeds/base_rip_3r3sw6pc.pkt")
OUT = Path("out")

# 这台 Router-PT-Empty 插了串口模块，局域网口是 Fa2/0、Fa3/0（不是 Fa0/0）
R1_CONFIG_FRESH = """version 12.2
hostname R1-EDGE
no ip cef
interface Serial0/0
 ip address 10.99.0.1 255.255.255.252
 no shutdown
interface FastEthernet2/0
 ip address 192.168.16.1 255.255.255.192
 no shutdown
router rip
 version 2
 network 10.0.0.0
 network 192.168.16.0
 no auto-summary
line con 0
line vty 0 4
 login
end"""

PC_A_IP = ("192.168.16.10", "255.255.255.192", "192.168.16.62")


def patch_config(lines: list[str]) -> list[str]:
    """在种子原有配置上做最小改动：结构与命令集合原封不动。"""
    out: list[str] = []
    for ln in lines:
        s = ln.strip()
        if s.startswith("hostname"):
            ln = "hostname R1-EDGE"
        elif s == "ip address 10.0.0.1 255.0.0.0":
            ln = " ip address 10.0.0.29 255.255.255.252"
        elif s == "ip address 192.168.1.1 255.255.255.0":
            ln = " ip address 192.168.16.1 255.255.255.192"
        out.append(ln)
    idx = next((i for i, x in enumerate(out) if x.strip() == "router rip"), None)
    if idx is not None:
        out[idx + 1 : idx + 2] = [
            " version 2",
            " no auto-summary",
            " network 10.0.0.0",
            " network 192.168.16.0",
        ]
    return out


def main() -> None:
    OUT.mkdir(exist_ok=True)
    seed_xml = read_pt_file(SEED)

    p0 = Pkt.open(SEED)
    p0.save(OUT / "gate0_roundtrip.pkt")
    print(f"[gate0] 原样重编码      字节一致={(OUT / 'gate0_roundtrip.pkt').read_bytes() == SEED.read_bytes()}")

    p1 = Pkt.open(SEED)
    p1.by_name("Router0").rename("R1-EDGE")
    p1.by_name("PC0").rename("PC-A")
    p1.save(OUT / "gate1_rename.pkt")
    print("[gate1] 只改名          回读 OK")

    p2 = Pkt.open(SEED)
    p2.by_name("Router0").rename("R1-EDGE").set_config(R1_CONFIG_FRESH.splitlines()).move(200, 260)
    p2.by_name("PC0").rename("PC-A").set_host_ip(*PC_A_IP).move(120, 340)
    p2.clear_command_logs()
    p2.save(OUT / "gate2_config.pkt")
    pc = Pkt.open(OUT / "gate2_config.pkt").by_name("PC-A")
    print(f"[gate2] 注入+改主机     ip={pc.ip} gw={re.findall(r'<GATEWAY>([^<]*)</GATEWAY>', pc.block)}")

    p3 = Pkt.open(SEED)
    for victim in ("PC2", "PC3", "PC4", "PC5"):
        p3.remove_device(victim)
    p3.save(OUT / "gate3_pruned.pkt")
    b3 = Pkt.open(OUT / "gate3_pruned.pkt")
    print(f"[gate3] 删设备          {len(b3.blocks)} 台 / {len(b3.links())} 链路")

    # ---- gate4：最小改动（推荐路线）----
    p4 = Pkt.open(SEED)
    r0 = p4.by_name("Router0")
    r0.rename("R1-EDGE").set_config(patch_config(r0.config_lines))
    p4.clear_command_logs()
    p4.save(OUT / "gate4_patched.pkt")
    back4 = Pkt.open(OUT / "gate4_patched.pkt").by_name("R1-EDGE")
    print(f"[gate4] 原配置打补丁    {len(back4.config_lines)} 行")
    for ln in back4.config_lines:
        if ln.strip().startswith(("hostname", "ip address", "version", "network", "no auto")):
            print(f"          {ln}")

    # ---- gate5：整段替换（正确接口名）----
    p5 = Pkt.open(SEED)
    p5.by_name("Router0").rename("R1-EDGE").set_config(R1_CONFIG_FRESH.splitlines())
    p5.clear_command_logs()
    p5.save(OUT / "gate5_fresh.pkt")
    print(f"[gate5] 全新配置        {len(Pkt.open(OUT / 'gate5_fresh.pkt').by_name('R1-EDGE').config_lines)} 行")

    # ---- gate6：新建链路（把链式骨架补成三角形）----
    p6 = Pkt.open(SEED)
    p6.add_link(("Router0", "Serial1/0"), ("Router2", "Serial0/0"), cable="serial")
    p6.clear_command_logs()
    p6.save(OUT / "gate6_newlink.pkt")
    print(f"[gate6] 新建串行链路    {len(Pkt.open(OUT / 'gate6_newlink.pkt').links())} 条（原 11 → 12）")


if __name__ == "__main__":
    main()
