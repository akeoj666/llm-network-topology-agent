"""一键生成向导：双击 start.cmd 进入，不用记任何命令。

三件事：
  1. 进来先体检（Python 依赖、DeepSeek key），有问题直接给中文解法
  2. 菜单选功能，选完只看结果
  3. 生成完直接把 out 目录弹出来
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pktlab import cmdguard, nl, pipeline  # noqa: E402
from pktlab.dsl import Topology  # noqa: E402

BAR = "=" * 52
LINE = "-" * 52


def title(text: str) -> None:
    print()
    print(BAR)
    print(f"  {text}")
    print(BAR)


def _w(text: str) -> int:
    """显示宽度：中文算两格，否则 <10 的对齐会歪。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def kv(label: str, value: str) -> None:
    print(f"  {label}{' ' * max(1, 12 - _w(label))}{value}")


# ---------------------------------------------------------------- 剪贴板
def clipboard_text() -> str:
    """读剪贴板。tkinter 最稳，不依赖外部命令的编码。"""
    try:
        import tkinter

        root = tkinter.Tk()
        root.withdraw()
        try:
            return (root.clipboard_get() or "").strip()
        finally:
            root.destroy()
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
            capture_output=True,
            timeout=8,
        ).stdout
        for enc in ("utf-8", "gbk"):
            try:
                return out.decode(enc).strip()
            except UnicodeDecodeError:
                continue
    except Exception:
        pass
    return ""


def open_folder(path: Path) -> None:
    if os.environ.get("WB_NO_OPEN") == "1":  # 自动化测试时别弹资源管理器
        return
    try:
        os.startfile(str(path))  # noqa: S606  (Windows 专用，就是要开资源管理器)
    except Exception:
        print(f"  （自动打开失败，手动去 {path} 看吧）")


# ---------------------------------------------------------------- 体检
def check_env() -> tuple[bool, str]:
    """返回 (能否用 NL 通道, key 或用错误说明)。"""
    try:
        return True, nl.load_api_key()
    except nl.NLGenError as exc:
        return False, str(exc)


def require_key() -> str | None:
    ok, msg = check_env()
    if ok:
        return msg
    print()
    print("  [!] 还没配 DeepSeek key，一句话生成用不了。")
    print()
    for line in msg.splitlines():
        print("      " + line)
    print()
    print("  依次执行：")
    print(r'      notepad D:\bouns\.env')
    print("      把最后一行 sk-xxxxxxxx… 换成你的 key，保存关闭")
    print()
    print("  （不想配也行，先用菜单 [2] 看内置示例。）")
    return None


# ---------------------------------------------------------------- 功能
def do_nl(prompt: str | None = None) -> None:
    key = require_key()
    if key is None:
        return

    if not prompt:
        print()
        print("  请输入一句话需求。")
        print("  提示：可以先把需求复制到剪贴板，这里直接按回车，会自动读剪贴板。")
        print()
        try:
            prompt = input("  需求> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not prompt:
            prompt = clipboard_text()
            if prompt:
                print(f"  已从剪贴板读取：{prompt[:70]}{'…' if len(prompt) > 70 else ''}")
    if not prompt:
        print("  [!] 没读到内容，取消。")
        return

    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    import nlgen  # 复用编号分配逻辑

    uid = nlgen.next_id(out)
    print(f"\n  用例编号自动分配为 {uid}（可用 --id 指定）\n")

    client = nl.Client(key=key)
    t0 = time.time()
    try:
        res = nl.translate(prompt, client, max_rounds=3, id_hint=uid)
    except nl.NLGenError as exc:
        print(f"\n  [X] 调用失败：{exc}")
        print("      检查：key 是否正确、账户是否还有余额、网络是否通。")
        return
    elapsed = time.time() - t0

    if res.yaml_text:
        (out / f"{uid}.yaml").write_text(res.yaml_text, encoding="utf-8")

    if res.topology is None:
        print(f"\n  [X] {len(res.rounds)} 轮仍未通过校验。")
        print(f"      最后一版 DSL 在 {out / (uid + '.yaml')}，可手工改完走菜单 [3]。")
        return

    print(f"\n  [√] 校验通过，{elapsed:.1f}s，共 {len(res.rounds)} 轮")
    try:
        p = pipeline.run(res.topology, out)
    except cmdguard.CmdBlocked as exc:
        print("\n  [X] 命令级护栏拦截，未生成 pkt：")
        print(exc)
        return
    except cmdguard.ConfirmRefused as exc:
        print(f"\n  [!] 已中止：{exc}")
        return
    show_products(res.topology, p)
    open_folder(out)


def do_examples() -> None:
    """不联网，把 examples/ 里现有的用例全部重生成一遍。"""
    files = sorted((ROOT / "examples").glob("uc*.yaml"))
    files = [f for f in files if "bad" not in f.name]
    if not files:
        print("  [!] examples/ 里没有可用的 YAML。")
        return
    out = ROOT / "out"

    # 批量生成也过一次确认门：只问一次，输对了才继续；不提供静默路径
    print()
    print(f"  即将重新生成 {len(files)} 个内置示例（本地模板，不含模型输出）：")
    print("    " + "、".join(f.stem for f in files))
    try:
        answer = input(f"  输入大写 {cmdguard.CONFIRM_WORD} 继续：").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer != cmdguard.CONFIRM_WORD:
        print("  已取消，未生成任何文件。")
        return
    batch_confirmer = cmdguard.static_confirm(
        f"交互菜单确认（一次性批准 {len(files)} 个内置示例）"
    )

    ok = 0
    for f in files:
        topo = Topology.load(f)
        topo.validate()
        if not topo.ok:
            print(f"  [X] {f.name} 校验不过：")
            print(topo.report())
            continue
        try:
            p = pipeline.run(topo, out, confirmer=batch_confirmer)
        except cmdguard.CmdBlocked as exc:
            print(f"  [X] {f.stem} 被命令级护栏拦截：")
            print(exc)
            continue
        print(f"  [√] {topo.meta.get('id')}  {topo.meta.get('title')}  ->  {p.pkt.name}")
        ok += 1
    print(f"\n  完成 {ok}/{len(files)} 个。")
    open_folder(out)


def do_file() -> None:
    path = input("  YAML 路径（可直接把文件拖进窗口）> ").strip().strip('"')
    if not path:
        return
    f = Path(path)
    if not f.exists():
        print(f"  [!] 找不到文件：{f}")
        return
    topo = Topology.load(f)
    topo.validate()
    if not topo.ok:
        print("  [X] 校验未通过：")
        print(topo.report())
        return
    out = ROOT / "out"
    try:
        p = pipeline.run(topo, out)
    except cmdguard.CmdBlocked as exc:
        print("  [X] 命令级护栏拦截，未生成 pkt：")
        print(exc)
        return
    except cmdguard.ConfirmRefused as exc:
        print(f"  [!] 已中止：{exc}")
        return
    show_products(topo, p)
    open_folder(out)


def show_products(topo, p) -> None:
    print()
    print(LINE)
    kv("用例", f"{topo.meta.get('id')}  {topo.meta.get('title')}")
    kv("设备", f"{len(topo.devices)} 台 / 链路 {len(topo.links)} 条")
    print(LINE)
    for label, path in (
        ("pkt", p.pkt),
        ("拓扑图", p.png),
        ("配置", p.configs),
    ):
        kv(label, path.name)
    print(LINE)
    print(f"  全部产物在：{p.pkt.parent}")


def do_accept() -> None:
    """对 out/ 和 examples/ 里的用例做验收（静态推算，不启动 PT）。"""
    from accept import main as accept_main  # 同目录的 tools/accept.py

    print()
    accept_main(["--all"])
    open_folder(ROOT / "out")


def do_report() -> None:
    """重算 D 量化评测表。所有数字现场算，不依赖历史结论。"""
    import subprocess

    print()
    r = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("report_d.py"))],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    for line in (r.stdout or "").strip().splitlines():
        print(f"  {line}")
    if r.returncode != 0:
        print(r.stderr or "(无输出)")
    print(f"  报告：{ROOT / 'out' / 'D_量化评测.md'}")


def do_selftest() -> None:
    """不联网的自测：故障注入闭环 + 验收器反向测试。"""
    import subprocess

    py = sys.executable
    for script, title in (
        ("test_faults.py", "故障注入闭环（24 条）"),
        ("test_accept.py", "验收器反向测试"),
        ("test_nl.py", "生成闭环（含护栏）"),
    ):
        print()
        print(f"---- {title} ----")
        r = subprocess.run(
            [py, str(Path(__file__).with_name(script))],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        tail = (r.stdout or "").strip().splitlines()
        for line in tail[-4:]:
            print(f"  {line}")
        if r.returncode != 0:
            print(f"  [!] {script} 未通过，完整输出：")
            print(r.stdout or r.stderr)


# ---------------------------------------------------------------- 菜单
def menu() -> str:
    print()
    print(LINE)
    print("  [1] 一句话生成           输入中文需求，自动出 pkt（要 key）")
    print("  [2] 重新生成内置示例     不联网，UC-01/02/03/05 一次刷完")
    print("  [3] 从 YAML 生成         已有 DSL 文件的情况")
    print("  [4] 验收全部用例         算连通性矩阵 + 一致性检查，出报告")
    print("  [5] 闭环自测             故障注入 / 护栏 / 验收，不联网")
    print("  [6] 生成评测表           重算 D 量化指标 → out\\D_量化评测.md")
    print("  [7] 打开 out 目录")
    print("  [Q] 退出")
    print(LINE)
    try:
        return input("  选择 [1]: ").strip().lower() or "1"
    except (EOFError, KeyboardInterrupt):
        return "q"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # 真·一键：把需求复制到剪贴板，双击 quick.cmd，直接出结果
    if "--clip" in sys.argv:
        title("一键生成（需求取自剪贴板）")
        text = clipboard_text()
        if not text:
            print("  [!] 剪贴板里没有文字。")
            print("      先把需求复制一下，再双击 quick.cmd。")
            return 1
        print(f"  读到需求：{text[:80]}{'…' if len(text) > 80 else ''}")
        do_nl(text)
        return 0

    title("一句话 → 拓扑图 + Packet Tracer 工程")

    ok, msg = check_env()
    if ok:
        kv("DeepSeek", f"已配置（{msg[:6]}…{msg[-4:]}）")
    else:
        kv("DeepSeek", "未配置（只能用菜单 2/3）")
    kv("输出目录", str(ROOT / "out"))

    while True:
        choice = menu()
        if choice in ("q", "quit", "exit"):
            print("  再见。")
            return 0
        if choice == "1":
            do_nl()
        elif choice == "2":
            do_examples()
        elif choice == "3":
            do_file()
        elif choice == "4":
            do_accept()
        elif choice == "5":
            do_selftest()
        elif choice == "6":
            do_report()
        elif choice == "7":
            open_folder(ROOT / "out")
        else:
            print("  没这个选项，重新选。")


if __name__ == "__main__":
    sys.exit(main())
