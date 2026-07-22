#!/usr/bin/env python3
"""PreToolUse 门：按 `.active-role` 裁决工具调用，而不是每次去问用户。

读 stdin 上的 hook payload，在 stdout 上给出 permissionDecision：

  allow — 在当前角色职权内，不打扰用户
  deny  — 越过角色红线或自提权，并指明该请求哪个 ROLE SWITCH
  ask   — 在职权内但不可逆/外向，交人确认

覆盖两类红线：

1. **写权矩阵**（散文正本在仓库根 CLAUDE.md「Research role workflow」）。
   `util/roles/use-role` 的 chmod 门只盖到文档区，且 `sed -i` / `rm`+重建能绕过它
   （rename(2)/unlink(2) 要的是目录权限，不是文件权限）。本脚本补齐 `src/`、
   `configs/`、`tests/`，以及跨工作树的写入。

2. **本项目特有的操作纪律**（CLAUDE.md「Primary research goal」）：并行 gem5 进程
   禁发 SIGUSR1/2、隔离核 54-55 / 92-111 专属实验员、`scons -j` 不得吃满整机、
   gem5 输出目录不得落进仓库。这些违反一次就作废一批数据或别人的实验。

失败安全方向：解析不了的一律 `ask`，绝不 `allow`。脚本自身崩溃则钩子被跳过，
harness 回落到自己的确认提示——用户仍然看得到这次调用。

这是护栏不是沙箱：足够间接的写法（python -c 打开文件写、shell 函数、仓库内种一个
软链）能绕过路径检查。它的假想敌是漂移和手滑，和 chmod 门、协议文字同一量级。
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path

MAIN_ROLES = {"pi", "architect"}
WT_ROLES = {"researcher", "experimenter", "implementor", "debugger"}
ROLES = MAIN_ROLES | WT_ROLES

# 内核隔离核（/proc/cmdline 的 isolcpus=）：54-55 归串行臂，92-111 归并行臂，
# 只有实验员可以碰。构建工具占用它们会污染正在跑的 A/B 计时。
RESERVED_CPUS = {54, 55} | set(range(92, 112))

# 写权矩阵的可执行副本。先匹配者胜，所以更具体的条目必须排在前面。
MAIN_AREAS: list[tuple[str, set[str]]] = [
    ("docs/specs/INDEX.md", {"pi"}),
    ("docs/specs/OPEN-ISSUES.md", {"pi"}),
    ("docs/roadmap/", {"pi"}),
    ("docs/specs/", {"architect"}),
    ("docs/decisions/", {"architect"}),
    ("docs/roles/", {"architect"}),
    ("CLAUDE.md", {"architect"}),
    (".claude/", {"architect"}),
    ("util/roles/", {"architect"}),
    ("src/", set()),
    ("configs/", set()),
    ("build_opts/", set()),
    ("tests/", set()),
    (".active-role", set()),
]

WT_AREAS: list[tuple[str, set[str]]] = [
    ("docs/specs/INDEX.md", set()),
    ("docs/specs/OPEN-ISSUES.md", set()),
    ("docs/specs/", {"researcher", "experimenter", "implementor", "debugger"}),
    ("docs/roadmap/", set()),
    ("docs/decisions/", set()),
    ("docs/roles/", set()),
    ("CLAUDE.md", set()),
    (".claude/", set()),
    ("util/roles/", set()),
    ("docs/refs/scripts/", {"implementor"}),
    ("src/", {"implementor", "debugger"}),
    ("configs/", {"implementor"}),
    ("build_opts/", {"implementor"}),
    ("tests/", {"implementor", "debugger"}),
    (".active-role", set()),
]

# deny 消息里「该找谁」的一半。
ROUTE = {
    "docs/specs/INDEX.md": "pi",
    "docs/specs/OPEN-ISSUES.md": "pi",
    "docs/roadmap/": "pi",
    "docs/specs/": "architect",
    "docs/decisions/": "architect",
    "docs/roles/": "architect",
    "CLAUDE.md": "architect",
    ".claude/": "architect",
    "util/roles/": "architect",
    "docs/refs/scripts/": "implementor",
    "src/": "implementor",
    "configs/": "implementor",
    "build_opts/": "implementor",
    "tests/": "implementor",
}

# 仓库外但属于本项目正常产物的位置：实验输出、tmpfs build、checkpoint、二进制。
# 这些不受写权矩阵管——研究流程本来就要求 gem5 的 -d 指到 /tmp。
OUTSIDE_OK = (
    "/workspace/shm/",
    "/workspace/gem5-ckpt/",
    "/workspace/gem5-bin/",
    "/workspace/gem5-resources/",
)
MEMORY_DIR = re.compile(
    re.escape(os.path.expanduser("~/.claude/projects")) + r"/[^/]+/memory/"
)

MAIN_TREE = "/workspace/gem5"
WT_PREFIX = "/workspace/gem5-wt/"

# ---- 命令层的红线 ------------------------------------------------------------
# 所有模式都对**命令位**匹配（heredoc 正文已剥离、前导 env=VAL 已剥离），而不是
# 对整条命令串。否则 `git commit -m "...禁止 kill -USR1..."` 或 `grep -rn "kill -USR1"`
# 这种只是**提到**红线词的调用会被误杀。

SIGUSR = re.compile(
    r"^(?:kill|pkill|killall)\b[^|;&]*?"
    r"(?:-{1,2}(?:SIG)?USR[12]\b|-s\s+(?:SIG)?USR[12]\b|-s\s+1[02]\b|-(?:10|12)\b)",
    re.IGNORECASE,
)

HARD_DENY: list[tuple[re.Pattern[str], str]] = [
    (
        SIGUSR,
        "禁止对 gem5 进程发 SIGUSR1/SIGUSR2：并行 EventQueue 模式下会在无 GIL 的"
        "非主线程做 stat dump，直接段错误。要读实时 tick 见 "
        "docs/specs/S-007-spin-barrier-and-milestone.md §14。",
    ),
    (
        re.compile(r"^chmod\b[^|;&]*(?:docs/|CLAUDE\.md|\.active-role|util/roles|\.claude)"),
        "改写权门本身 = 自提权。角色写权只由 util/roles/use-role 设定，不得手工 chmod。",
    ),
    (
        re.compile(r"(?:>|>>)\s*\S*\.active-role|^tee\b[^|;&]*\.active-role"
                   r"|^sed\b[^|;&]*-i[^|;&]*\.active-role"),
        "`.active-role` 是权限凭证，只能由 util/roles/use-role 写入——直接改它等于自选角色。",
    ),
    (
        re.compile(r"^git\s+push\b"),
        "CLAUDE.md：推送到 origin 一律由用户手工执行，任何角色都不代劳。",
    ),
    (re.compile(r"^rm\s+(?:-[a-zA-Z]+\s+)*/(?:\s|$)"), "rm 作用于根目录。"),
]

ASK: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^sudo\b"), "sudo 越出仓库边界（本沙盒装构建依赖时确实需要，故交人确认）。"),
    (
        re.compile(r"^git\s+remote\s+(?:add|set-url|remove)\b"),
        "改 remote 是外向且不可逆的动作。",
    ),
    (
        re.compile(r"^git\s+commit\b[^|;&]*(?:--no-verify|\s-n\b)|^git\s+commit\b[^|;&]*--amend"),
        "绕过提交钩子 / 改写历史，需人在场。",
    ),
    (
        re.compile(r"^git\s+(?:reset\s+--hard|clean\s+-[a-zA-Z]*f)\b"),
        "会丢弃未提交的工作。",
    ),
]

# `git` 里只有 PI 能做的动作：造分支/worktree、合并回主干。
PI_ONLY_GIT = re.compile(
    r"^git\s+(?:merge\b|worktree\s+add\b|checkout\s+-b\b|switch\s+-c\b"
    r"|branch\s+(?!-[avlr]|--list|--show|--contains|-d\b|-D\b))"
)

SCONS = re.compile(r"\bscons\b")
SCONS_J = re.compile(r"-j\s*\d+|-j\d+")
PINNED = re.compile(r"^(?:taskset|numactl)\b")
GEM5_BIN = re.compile(r"\bgem5\.(?:opt|debug|fast|prof|perf)\b")
GEM5_OUTDIR = re.compile(r"(?:^|\s)(?:-d|--outdir)(?:[=\s]+)(\S+)")
CPU_LIST = re.compile(r"(?:taskset\s+(?:-c|--cpu-list)|numactl\s+(?:-C|--physcpubind=?))\s*=?\s*([0-9,\-]+)")

HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1.*?^\s*\2\s*$", re.DOTALL | re.MULTILINE)
ENV_PREFIX = re.compile(r"^(?:\w+=\S*\s+)+")
WRAPPERS = re.compile(r"^(?:nohup\s+|time\s+|exec\s+|stdbuf\s+\S+\s+)+")
SEGMENT_SPLIT = re.compile(r"(?:\|\||&&|[;|&\n])+")

# 路径参数就是写目标的命令。`sed -i` 和 `rm`+重建都能绕开 chmod，这份清单是补丁。
PATH_WRITERS = re.compile(r"^(?:sed\s+[^|;&]*-i|tee|rm|mv|truncate|dd)\b")
DEST_ONLY_WRITERS = re.compile(r"^(?:cp|install|rsync)\b")
REDIRECTS = re.compile(r"(?<![0-9<>])>>?\s*([^\s;|&<>()]+)")


def emit(decision: str, reason: str = "") -> None:
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.exit(0)


def repo_root() -> Path:
    return Path(os.environ.get("CLAUDE_PROJECT_DIR") or Path.cwd()).resolve()


def tree_kind(root: Path) -> str:
    """主树的 .git 是目录，linked worktree 的 .git 是文件。与路径无关。"""
    return "main" if (root / ".git").is_dir() else "worktree"


def expand_cpus(spec: str) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                out.update(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                out.add(int(part))
            except ValueError:
                continue
    return out


def area_for(rel: str, tree: str) -> tuple[str, set[str]] | None:
    for prefix, owners in MAIN_AREAS if tree == "main" else WT_AREAS:
        if rel == prefix or rel.startswith(prefix):
            return prefix, owners
    return None


def deny_reason(prefix: str, owners: set[str], role: str, tree: str) -> str:
    where = "主树" if tree == "main" else "worktree"
    if not owners:
        return (
            f"`{prefix}` 在 {where} 里不对任何角色开放写权"
            + (
                "（INDEX/OPEN-ISSUES 只由 PI 在主树维护，分支里改必然引发合并冲突）"
                if "INDEX" in prefix or "OPEN-ISSUES" in prefix
                else "（这是另一棵树的写权区）"
            )
        )
    route = ROUTE.get(prefix) or sorted(owners)[0]
    return (
        f"`{prefix}` 在 {where} 里只对 {'/'.join(sorted(owners))} 开放写权，当前角色是 {role}。"
        f"需要就请求 `ROLE SWITCH: {route} — <理由>`（CLAUDE.md 角色切换）"
    )


def check_path(raw: str, root: Path, role: str, tree: str) -> tuple[str, str] | None:
    """`raw` 若是当前角色不该写的目标，返回 (decision, reason)。

    走的是**词法**归一（normpath），不是 resolve()——resolve 会跟着 build/ 的 tmpfs
    软链跑出仓库，把实验员自己的活判成越界。代价是仓库内种下的软链能绕过检查，
    这正是模块 docstring 里划的护栏/沙箱那条线。
    """
    raw = os.path.expanduser(raw.strip("\"'"))
    root_s = str(root)
    target = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(root_s, raw))

    if target in {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"} or target.startswith("/dev/fd/"):
        return None

    if target.startswith(os.path.realpath(tempfile.gettempdir()) + os.sep) or target.startswith("/tmp/"):
        return None
    if target.startswith(OUTSIDE_OK) or MEMORY_DIR.match(target):
        return None

    if target == root_s or target.startswith(root_s + os.sep):
        hit = area_for(os.path.relpath(target, root_s), tree)
        if hit is None:
            return None
        prefix, owners = hit
        return None if role in owners else ("deny", deny_reason(prefix, owners, role, tree))

    # 跨工作树写入：破坏「一个 worktree 一个研究」的隔离，也是合并冲突的源头。
    if target == MAIN_TREE or target.startswith(MAIN_TREE + os.sep):
        return (
            "deny",
            f"`{target}` 在主树里，当前会话在 {root_s}。主树只对 pi/architect 开放，"
            "且必须在主树的会话里改——跨树写入会绕开写权门。",
        )
    if target.startswith(WT_PREFIX):
        return ("deny", f"`{target}` 属于另一个 worktree。一个 worktree 一个研究，不得跨树写。")

    return ("ask", f"`{target}` 在仓库和已知产物目录之外。")


def segments(cmd: str) -> list[tuple[str, str]]:
    """返回 (env 剥离后的段, 再剥掉 nohup/time 等包装后的段)。

    taskset/numactl **不**算包装——它们承载绑核信息，必须留在第一个元素里。
    """
    out = []
    for raw in SEGMENT_SPLIT.split(HEREDOC.sub("", cmd)):
        seg = ENV_PREFIX.sub("", raw.strip())
        if seg:
            out.append((seg, WRAPPERS.sub("", seg)))
    return out


def check_discipline(seg: str, root: Path, role: str, tree: str) -> tuple[str, str] | None:
    """本项目特有的实验操作纪律。"""
    # 隔离核：只有实验员可以碰。
    m = CPU_LIST.search(seg)
    if m:
        hit = expand_cpus(m.group(1)) & RESERVED_CPUS
        if hit and role != "experimenter":
            return (
                "deny",
                f"核 {sorted(hit)} 是内核隔离的 A/B 计时专用核（54-55 串行臂 / 92-111 并行臂），"
                f"只对 experimenter 开放，当前角色是 {role}。构建请限制到非保留核（上限 90）。",
            )

    if SCONS.search(seg):
        if tree == "main":
            return ("deny", "主树是研究主干，不是实验场：构建只在 worktree 里做（CLAUDE.md）。")
        if role in {"researcher", "experimenter"}:
            who = "implementor" if role == "researcher" else "implementor"
            return (
                "deny",
                f"{role} 不做构建。需要重建请求 `ROLE SWITCH: {who} — <理由>`。",
            )
        if SCONS_J.search(seg) and not PINNED.match(seg):
            return (
                "deny",
                "`scons -j` 会吃满整机，包括隔离核 54-55 / 92-111，污染正在跑的 A/B 计时。"
                "必须用 taskset/numactl 限制到非保留核，例如 `taskset -c 0-53,56-91 scons -j80 ...`。",
            )

    if GEM5_BIN.search(seg):
        if tree == "main":
            return ("deny", "主树不跑 gem5：实验只在 worktree 里做（CLAUDE.md）。")
        if role == "researcher":
            return (
                "deny",
                "researcher 不跑实验——只读探查可以，一旦要计时或采数就是实验员的活。"
                "请求 `ROLE SWITCH: experimenter — <理由>`。",
            )
        # 输出目录必须在仓库外：cpt.*/ 和 m5out 是大块二进制状态，不进仓库。
        od = GEM5_OUTDIR.search(seg)
        if od:
            p = os.path.normpath(os.path.join(str(root), os.path.expanduser(od.group(1).strip("\"'"))))
            if p == str(root) or p.startswith(str(root) + os.sep):
                return (
                    "deny",
                    f"gem5 输出目录 `{od.group(1)}` 落在仓库内。CLAUDE.md：始终 `-d /tmp/<...>`，"
                    "checkpoint 是大块二进制状态，不该进仓库。",
                )
        elif re.search(r"\S+\.py(?:\s|$)", seg):
            return (
                "deny",
                "跑 gem5 配置脚本必须显式给 `-d /tmp/<...>`：默认 outdir 是 cwd 下的 m5out，"
                "会把输出和 checkpoint 落进仓库（CLAUDE.md Checkpoints）。",
            )

    if PI_ONLY_GIT.match(seg) and role != "pi":
        return (
            "deny",
            "建分支/worktree 和合并回主干是 PI 的职权（CLAUDE.md 生命周期第 3、5 步）。"
            "请求 `ROLE SWITCH: pi — <理由>`，或由用户直接执行。",
        )

    return None


def gate_bash(cmd: str, root: Path, role: str, tree: str) -> tuple[str, str]:
    segs = segments(cmd)

    for seg, _ in segs:
        for pat, why in HARD_DENY:
            if pat.search(seg):
                return ("deny", why)

    for seg, bare in segs:
        verdict = check_discipline(seg, root, role, tree) or check_discipline(bare, root, role, tree)
        if verdict:
            return verdict

    # 职权先于不可逆性：本就无权做的事给 deny，而不是给一个邀请用户确认越界的 ask。
    for seg, bare in segs:
        targets: list[str] = list(REDIRECTS.findall(seg))
        args = [t for t in bare.split()[1:] if not t.startswith("-")]
        if PATH_WRITERS.match(bare):
            targets += args
        elif DEST_ONLY_WRITERS.match(bare) and args:
            targets.append(args[-1])
        for raw in targets:
            verdict = check_path(raw, root, role, tree)
            if verdict:
                return verdict

    for seg, bare in segs:
        for pat, why in ASK:
            if pat.search(bare):
                return ("ask", why)

    return ("allow", f"{role} 职权内")


def main() -> None:
    payload = json.load(sys.stdin)
    tool = payload.get("tool_name", "")
    ti = payload.get("tool_input") or {}
    root = repo_root()
    tree = tree_kind(root)

    role_file = root / ".active-role"
    role = role_file.read_text().strip() if role_file.is_file() else ""
    if role not in ROLES:
        emit("ask", f"`.active-role` 缺失或不是已知角色（读到 {role!r}）—— 先跑 util/roles/use-role <role>")

    expected = MAIN_ROLES if tree == "main" else WT_ROLES
    if role not in expected:
        emit(
            "ask",
            f"角色 {role} 与工作树不匹配（{root} 是 {tree} 树，只接受 {'/'.join(sorted(expected))}）"
            "—— 先跑 util/roles/use-role 重新激活",
        )

    if tool == "Bash":
        cmd = ti.get("command", "")
        if not cmd:
            emit("ask", "Bash 调用没有 command 字段")
        emit(*gate_bash(cmd, root, role, tree))

    if tool in {"Edit", "Write", "NotebookEdit"}:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not path:
            emit("ask", f"{tool} 调用没有 file_path")
        emit(*(check_path(path, root, role, tree) or ("allow", f"{role} 职权内")))

    emit("ask", f"role-gate 未覆盖工具 {tool!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 失败安全：暴露出来，绝不静默放行
        emit("ask", f"role-gate 内部错误：{type(exc).__name__}: {exc}")
