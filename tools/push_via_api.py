"""当 `git push` 用不了时，用 GitHub REST API 把本地提交「原样重放」到远端。

为什么需要：这台机器上 `github.com:443` 不通（`api.github.com` 通），
git over HTTPS 直接报 `CONNECT tunnel failed, response 502`。内容本身可以走 API。

做法：对 `--from..HEAD` 之间的每条本地提交，依次
    读取本地 author/committer/date/message → 建 blob → 建 tree（base_tree=上一条的 tree）
    → 建 commit（parents=[上一条的远端 sha]）→ 最后 PATCH ref
因为元信息与 tree 完全一致，算出来的 commit SHA 与本地相同，远端与本地不会分叉。

用法：
    python tools/push_via_api.py --repo akeoj666/llm-network-topology-agent --from 47189b9
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import subprocess
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True,
    ).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            return line.split("=", 1)[1]
    raise SystemExit("找不到 GitHub token")


def call(method: str, path: str, tok: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        body = r.read().decode()
    return json.loads(body) if body.strip() else {}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout.decode("utf-8")


def iso(ts_tz: str) -> str:
    ts, tz = ts_tz.split(" ")
    sign = 1 if tz[0] == "+" else -1
    off = sign * (int(tz[1:3]) * 60 + int(tz[3:5]))
    t = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc) + dt.timedelta(minutes=off)
    return t.strftime("%Y-%m-%dT%H:%M:%S") + tz[:3] + ":" + tz[3:5]


def ident(s: str) -> tuple[str, str, str]:
    # 形如：XH ZUO <a@b.c> 1792644239 +0800 —— 时间戳和时区之间还有一个空格
    name_email, ts, tz = s.rsplit(" ", 2)
    name, email = name_email.rsplit(" <", 1)
    return name, email.rstrip(">"), iso(f"{ts} {tz}")


def replay(commit: str, prev_remote: str | None, repo: str, tok: str,
           parents: list[str] | None = None) -> str:
    raw = git("cat-file", "-p", commit)
    body_start = raw.index("\n\n") + 2
    author = committer = ""
    for line in raw[:body_start].splitlines():
        if line.startswith("author "):
            author = line[len("author "):]
        elif line.startswith("committer "):
            committer = line[len("committer "):]
    # 提交对象里 message 自带一个结尾换行；GitHub 会自己补一个，
    # 不去掉就会变成两个换行，算出的 SHA 与本地不同（实测踩过）
    message = raw[body_start:].rstrip("\n")

    a_name, a_mail, a_date = ident(author)
    c_name, c_mail, c_date = ident(committer)

    if prev_remote is None:
        base_tree = None  # 根提交：把该提交里的所有文件都放进去
    else:
        base_tree = call("GET", f"/repos/{repo}/git/commits/{prev_remote}", tok)["tree"]["sha"]

    if prev_remote is None:
        # 根提交：列出该提交的全部文件
        listing = git("-c", "core.quotepath=false", "ls-tree", "-r", "--name-only", commit).splitlines()
        diff = [f"A\t{p}" for p in listing if p.strip()]
    else:
        diff = git("-c", "core.quotepath=false", "diff", "--name-status",
                   f"{commit}~1", commit).splitlines()
    entries = []
    for line in diff:
        status, path = line.split("\t", 1)
        path = path.strip()
        if status == "D":
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        # 从对象库里取该提交时的内容，不读工作区
        content = subprocess.run(["git", "show", f"{commit}:{path}"],
                                 capture_output=True, check=True).stdout
        blob = call("POST", f"/repos/{repo}/git/blobs", tok, {
            "content": base64.b64encode(content).decode(),
            "encoding": "base64",
        })
        mode = "100755" if git("ls-tree", commit, "--", path).startswith("100755") else "100644"
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": blob["sha"]})

    payload = {"tree": entries}
    if base_tree:
        payload["base_tree"] = base_tree
    tree = call("POST", f"/repos/{repo}/git/trees", tok, payload)
    new = call("POST", f"/repos/{repo}/git/commits", tok, {
        "message": message,
        "tree": tree["sha"],
        "parents": parents if parents is not None else [prev_remote],
        "author": {"name": a_name, "email": a_mail, "date": a_date},
        "committer": {"name": c_name, "email": c_mail, "date": c_date},
    })
    same = "✓ SHA 与本地一致" if new["sha"] == commit else "（内容相同、SHA 不同）"
    print(f"  {commit[:8]} → {new['sha'][:8]}  {len(entries)} 文件  {same}")
    return new["sha"]


def rebuild_all(args, tok: str, remote_head: str) -> int:
    """整条链重建：只换提交对象（作者身份/时间），树一律复用。

    改作者身份时**文件内容没变**，所以 tree sha 也一样，而且这些 tree 早就在远端了
    （前几轮推送时创建过）。于是只需要 22 次 POST /git/commits，
    不必重新上传任何 blob——之前那版逐文件重建，慢到跑不完。
    如果某个 tree 远端没有（极端情况），再退回慢速路径现场构建。
    """
    commits = git("rev-list", "--reverse", "HEAD").split()
    print(f"整链重建 {len(commits)} 条提交（复用已有 tree，不重传文件）：")
    prev = None
    for i, c in enumerate(commits, 1):
        raw = git("cat-file", "-p", c)
        body = raw.index("\n\n") + 2
        author = next(l[len("author "):] for l in raw[:body].splitlines() if l.startswith("author "))
        committer = next(l[len("committer "):] for l in raw[:body].splitlines() if l.startswith("committer "))
        message = raw[body:].rstrip("\n")
        a_name, a_mail, a_date = ident(author)
        c_name, c_mail, c_date = ident(committer)
        tree = git("rev-parse", f"{c}^{{tree}}").strip()

        payload = {
            "message": message,
            "tree": tree,
            "author": {"name": a_name, "email": a_mail, "date": a_date},
            "committer": {"name": c_name, "email": c_mail, "date": c_date},
        }
        if prev:
            payload["parents"] = [prev]
        try:
            new = call("POST", f"/repos/{args.repo}/git/commits", tok, payload)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()[:200]
            print(f"  [{i}] tree {tree[:8]} 远端不存在，退回构建：{detail}")
            prev = replay(c, prev, args.repo, tok,
                          parents=[] if prev is None else [prev])
        else:
            same = "✓ 与本地同 SHA" if new["sha"] == c else "（内容同，SHA 不同）"
            print(f"  [{i:2}/{len(commits)}] {c[:8]} → {new['sha'][:8]}  {same}")
            prev = new["sha"]

    call("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.ref}", tok,
         {"sha": prev, "force": True})
    print(f"已更新 refs/heads/{args.ref} → {prev[:8]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--from", dest="base", default="", help="远端当前所在提交（本地 sha）")
    ap.add_argument("--rebuild-all", action="store_true",
                    help="整条链重建（改作者身份用，树不变只重建 commit）")
    args = ap.parse_args()

    tok = token()
    remote_head = call("GET", f"/repos/{args.repo}/git/ref/heads/{args.ref}", tok)["object"]["sha"]
    if args.rebuild_all:
        return rebuild_all(args, tok, remote_head)

    if remote_head != git("rev-parse", args.base).strip():
        print(f"[注意] 远端 HEAD 是 {remote_head[:8]}，与 --from {args.base[:8]} 不同，从远端继续")
    commits = git("rev-list", "--reverse", f"{args.base}..HEAD").split()
    if not commits:
        print("没有需要推送的提交")
        return 0

    print(f"待重放 {len(commits)} 条提交：")
    prev = remote_head
    for c in commits:
        prev = replay(c, prev, args.repo, tok)

    call("PATCH", f"/repos/{args.repo}/git/refs/heads/{args.ref}", tok,
         {"sha": prev, "force": True})
    print(f"已更新 refs/heads/{args.ref} → {prev[:8]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
