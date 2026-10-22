"""自测：pkt 静态一致性校验器。

为什么要有这一套
----------------
「PT 打不开这个文件」是最难查的一类 bug。实验 8-1 的园区网第一次生成时
就踩了：设备块复制时只改了名字和 save-ref-id，而 PT 载入 .pkt 时是按
一组**身份标识**建索引的，克隆出来的设备跟模板设备共用同一批标识
（MEM_ADDR / DEV_ADDR / MAC / 物理视图 uuid）→ 索引冲突 → 拒绝加载。

查这个 bug 花的时间比写生成器还长。所以有了 ``tools/verify_pkt.py``：
把「PT 会拒绝的写法」变成静态可检查的规则，生成完立刻拦下。

这一套测试做两件事：
  ① **反向测试**：拿一份「已知有病」的 pkt 喂给校验器，必须报出对应错误。
     校验器不能只会说「通过」——它得真能抓出问题。
  ② **正向测试**：拿 PT 9.0.1 自己写的原生示例文件喂进去，必须不报错。
     规则是从那些文件反推出来的，所以对它们必须成立，否则就是误报。
"""

from __future__ import annotations

import glob
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import pkt  # noqa: E402
from tools.verify_pkt import verify  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  [ok  ] {label}")
    else:
        print(f"  [FAIL] {label}")
        FAILS.append(label)


def section(title: str) -> None:
    print(f"\n[{title}]")


# ---------------------------------------------------------------- 反向测试
def test_catches_duplicate_ids() -> None:
    section("反向：克隆设备共用身份标识要被抓出来")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad.pkt"
        # 拿我们的园区网种子当底本：它是干净的
        src = ROOT / "seeds" / "base_campus.pkt"
        raw = pkt.read_pt_file(src).decode("utf-8")

        # ① 把一台克隆设备的 SAVE_REF_ID 改成和另一台一样（丢掉前缀 + 撞 id）
        blocks = list(pkt.DEVICE_RE.finditer(raw))
        first = blocks[0].group(0)
        m = re.search(r"<SAVE_REF_ID>([^<]*)</SAVE_REF_ID>", first)
        assert m, "找不到 SAVE_REF_ID"
        # 找两台设备的块
        b0 = blocks[0]
        b1 = blocks[1]
        s0 = re.search(r"<SAVE_REF_ID>([^<]*)</SAVE_REF_ID>", b0.group(0)).group(1)
        # 篡改第 2 台设备：把它��� id 换成第 1 台的（共用身份），
        # 同时丢掉 save-ref-id: 前缀（第一版克隆的真实 bug）
        b1 = blocks[1]
        new1 = re.sub(
            r"<SAVE_REF_ID>save-ref-id:(\d+)</SAVE_REF_ID>",
            rf"<SAVE_REF_ID>{s0}</SAVE_REF_ID>",
            b1.group(0),
            count=1,
        )
        raw2 = raw[: b1.start()] + new1 + raw[b1.end() :]
        pkt.write_pt_file(p, raw2.encode("utf-8"))

        rep = verify(p)
        joined = " ".join(rep.errors)
        check(
            any("SAVE_REF_ID" in e for e in rep.errors),
            "缺 save-ref-id: 前缀被抓出来",
        )
        check(
            "SAVE_REF_ID 重复" in joined,
            "SAVE_REF_ID 重复（多台设备共用主 id）被抓出来",
        )


def test_catches_duplicate_mem_addr() -> None:
    section("反向：设备地址重复要被抓出来")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad2.pkt"
        src = ROOT / "seeds" / "base_campus.pkt"
        raw = pkt.read_pt_file(src).decode("utf-8")
        blocks = list(pkt.DEVICE_RE.finditer(raw))
        # 把第 2 台设备的 MEM_ADDR 改成第 1 台的
        mem0 = re.search(r"<MEM_ADDR>([^<]*)</MEM_ADDR>", blocks[0].group(0)).group(1)
        b1 = blocks[1].group(0)
        new1 = re.sub(
            r"<MEM_ADDR>[^<]*</MEM_ADDR>",
            f"<MEM_ADDR>{mem0}</MEM_ADDR>",
            b1,
            count=1,
        )
        raw2 = raw[: blocks[1].start()] + new1 + raw[blocks[1].end() :]
        pkt.write_pt_file(p, raw2.encode("utf-8"))
        rep = verify(p)
        check(
            any("MEM_ADDR 重复" in e for e in rep.errors),
            "MEM_ADDR 重复被抓出来",
        )


def test_catches_missing_physical_node() -> None:
    section("反向：物理视图缺节点要被抓出来")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "bad3.pkt"
        src = ROOT / "seeds" / "base_campus.pkt"
        raw = pkt.read_pt_file(src).decode("utf-8")
        # 把某台设备的 PHYSICAL 末位 uuid 改成一个不存在的
        blocks = list(pkt.DEVICE_RE.finditer(raw))
        b0 = blocks[0].group(0)
        new0 = re.sub(
            r"(<PHYSICAL>[^<]*,)\{[0-9a-f-]+\}(</PHYSICAL>)",
            r"\1{00000000-0000-0000-0000-000000000000}\2",
            b0,
            count=1,
        )
        raw2 = raw[: blocks[0].start()] + new0 + raw[blocks[0].end() :]
        pkt.write_pt_file(p, raw2.encode("utf-8"))
        rep = verify(p)
        check(
            any("物理视图里找不到" in e for e in rep.errors),
            "PHYSICAL 末位 uuid 对不上被抓出来",
        )


# ---------------------------------------------------------------- 正向测试
def test_native_pt_files_pass() -> None:
    section("正向：PT 9.0.1 自带的原生文件必须零误报")
    root = Path(r"D:\Cisco Packet Tracer 9.0.1\saves")
    files = glob.glob(str(root / "**" / "*.pkt"), recursive=True)
    if not files:
        print("  [跳过] 本机没有装 PT，找不到官方示例文件")
        return
    random.seed(20261022)
    sample = random.sample(files, min(40, len(files)))
    bad: list[str] = []
    for f in sample:
        rep = verify(f)
        if rep.errors:
            bad.append(Path(f).name)
    check(
        not bad,
        f"抽查 {len(sample)} 个 PT 原生文件，零误报"
        + (f"（误报：{bad[:3]}）" if bad else ""),
    )


def test_our_seeds_pass() -> None:
    section("正向：我们的种子与用例必须零错误")
    targets = [
        ROOT / "seeds" / "base_campus.pkt",
        ROOT / "seeds" / "base_ospf_3r3sw6pc.pkt",
        ROOT / "out" / "UC-83.pkt",
    ]
    for p in targets:
        if not p.exists():
            print(f"  [跳过] {p.name} 还没生成")
            continue
        rep = verify(p)
        check(
            not rep.errors,
            f"{p.name}：{len(rep.errors)} 个错误"
            + (f"（{rep.errors[0]}）" if rep.errors else ""),
        )


def main() -> int:
    print("=" * 60)
    print("pkt 一致性校验器自测")
    print("=" * 60)

    test_catches_duplicate_ids()
    test_catches_duplicate_mem_addr()
    test_catches_missing_physical_node()
    test_native_pt_files_pass()
    test_our_seeds_pass()

    print()
    if FAILS:
        print(f"FAILED：{len(FAILS)} 项未通过")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
