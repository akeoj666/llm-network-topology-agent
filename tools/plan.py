"""单独生成「IP/VLAN 编址规划书」。

正常流程是 `gen.py` 跑一次就把规划书一起产出；这个脚本是给它补跑用的，
或者只想要文字方案、不想动 pkt 时用。

    python tools/plan.py examples/uc01.yaml
    python tools/plan.py --all          :: examples/ 与 out/ 里的 yaml 全跑一遍
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import plan as plan_mod  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402


def write_one(yaml_path: Path, out_dir: Path) -> Path:
    topo = Topology.load(yaml_path)
    uid = topo.meta.get("id") or yaml_path.stem
    out = out_dir / f"{uid}_编址规划.md"
    out.write_text(plan_mod.render_markdown(plan_mod.build(topo)), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("yaml", nargs="*", help="DSL 文件；不给就配合 --all")
    ap.add_argument("--all", action="store_true", help="跑 examples/ 与 out/ 里的全部 yaml")
    ap.add_argument("--out", default=str(ROOT / "out"))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    files: list[Path] = [Path(f) for f in args.yaml]
    if args.all:
        files = sorted((ROOT / "examples").glob("uc*.yaml")) + sorted((ROOT / "out").glob("UC-*.yaml"))
        files = [f for f in files if f.name != "uc12_bad.yaml"]

    if not files:
        print("没给文件，也没加 --all")
        return 1

    for f in files:
        if not f.exists():
            print(f"[跳过] {f} 不存在")
            continue
        try:
            out = write_one(f, out_dir)
        except Exception as exc:
            print(f"[失败] {f.name}：{exc}")
            continue
        print(f"{f.name} → {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
