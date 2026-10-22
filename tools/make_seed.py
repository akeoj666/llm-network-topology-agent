"""扩容骨架：从现有种子复制设备块，造出设备更多的新种子。

为什么需要：实验 8-1 的园区网要 5 路由器 + 5 交换机 + 8 主机 + 3 服务器 = 21 台，
而现有骨架最多 12 台（3 路由 + 3 交换 + 6 主机）。而骨架是「只删不增」的——
生成器只会把骨架里的设备改名后拿去用，自己造不出新的设备块。

这里做的是**复制设备块**：
    ① 从种子里挑一个「样板」设备（按型号挑）
    ② 整块复制，给副本换上全新的 save-ref-id（必须唯一，否则 PT 认不出这是两台设备）
    ③ 副本的名字、坐标也改掉，避免和原设备撞名/重叠
    ④ 写回成新的种子文件

**注意**：这是本项目第一次做「设备块复制」。PT 到底接不接受复制出来的设备，
必须在 PT 里打开新种子实测一次才知道——所以这个脚本会把新种子写到
`seeds/` 下并提示人工验证，不直接进生成流水线。

    python tools/make_seed.py --out seeds/base_campus.pkt \
        --routers 5 --switches 5 --pcs 8 --servers 3
"""

from __future__ import annotations

import argparse
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import pkt  # noqa: E402

SRC = ROOT / "seeds" / "base_ospf_3r3sw6pc.pkt"

# 设备块里可能引用自己的 save-ref-id（多处），复制时要整体换掉
_REF_RE = re.compile(r"<SAVE_REF_ID>([^<]*)</SAVE_REF_ID>")
_TEXT_REF_RE = re.compile(r"save-ref-id:(\d+)")


def _new_ref(rng: random.Random) -> str:
    return str(rng.randrange(10**18, 10**20))


def clone_block(block: str, rng: random.Random) -> str:
    """复制一个设备块，并把里面所有指向它自己的 id 换成新 id。"""
    old = set(_TEXT_REF_RE.findall(block))
    new_id = _new_ref(rng)
    out = block
    for o in old:
        out = out.replace(f"save-ref-id:{o}", f"save-ref-id:{new_id}")
    # <SAVE_REF_ID> 是设备自身的主 id
    out = _REF_RE.sub(f"<SAVE_REF_ID>{new_id}</SAVE_REF_ID>", out, count=1)
    return out


_ENG_NAME_RE = re.compile(
    r"(<ENGINE>\s*<TYPE[^>]*>[^<]*</TYPE>\s*<NAME[^>]*>)([^<]*)(</NAME>)", re.S
)


def rename(block: str, old: str, new: str) -> str:
    """改设备名。名字在 <ENGINE> 里的 <NAME> 节点上。"""
    return _ENG_NAME_RE.sub(lambda m: m.group(1) + new + m.group(3), block, count=1)


def move(block: str, x: float, y: float) -> str:
    def rep(m: re.Match) -> str:
        return f"{m.group(1)}{x:g}{m.group(2)}{y:g}{m.group(3)}"
    return re.sub(r"(<LOGICAL_X>)([-\d.]+)(</LOGICAL_X>)", rep, block) if "LOGICAL_X" in block else block


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", default=str(SRC))
    ap.add_argument("--routers", type=int, default=0)
    ap.add_argument("--switches", type=int, default=0)
    ap.add_argument("--pcs", type=int, default=0)
    ap.add_argument("--servers", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20261022)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    src = Path(args.src)
    p = pkt.Pkt.open(src)
    devices = p.devices()
    if not devices:
        print("源种子里没有设备")
        return 1

    by_model: dict[str, list[str]] = {}
    for d in devices:
        by_model.setdefault(d.model, []).append(d.name)
    print("源种子设备：", {k: len(v) for k, v in by_model.items()})

    want = {
        "Router-PT-Empty": args.routers,
        "2960-24TT": args.switches,
        "PC-PT": args.pcs + args.servers,
    }

    extra: list[str] = []
    seq = 0
    for model, need in want.items():
        have = by_model.get(model) or []
        if not have:
            print(f"[警告] 源种子里没有 {model}，跳过")
            continue
        template = have[0]
        # 从种子里取出模板设备块
        tmpl_block = next(b for b in p.blocks
                          if _ENG_NAME_RE.search(b).group(2) == template)
        for i in range(len(have), need):
            seq += 1
            new_name = f"{model.split('-')[0]}_x{seq}"
            blk = clone_block(tmpl_block, rng)
            blk = rename(blk, template, new_name)
            extra.append(blk)
            print(f"  复制 {template} → {new_name}")

    if not extra:
        print("不需要扩容")
        return 0

    # 把新设备块插到最后一个设备块之后
    text = p.read() if hasattr(p, "read") else None
    raw = pkt.read_pt_file(src)
    raw = raw if isinstance(raw, str) else raw.decode("utf-8")
    blocks = list(pkt.DEVICE_RE.finditer(raw))
    if not blocks:
        print("没找到设备块")
        return 1
    last = blocks[-1]
    insert_at = last.end()
    raw = raw[:insert_at] + "".join(extra) + raw[insert_at:]

    out = Path(args.out)
    # .pkt 是加密容器（qCompress + Twofish-EAX），明文 XML 写进去 PT 打不开
    pkt.write_pt_file(out, raw.encode("utf-8"))
    print(f"已写出 {out}（新增 {len(extra)} 台设备，合计约 {len(blocks) + len(extra)} 台）")
    print("⚠️ 复制出来的设备块必须先在 PT 里打开这个种子确认能正常显示，"
          "再拿它去生成正式用例。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
