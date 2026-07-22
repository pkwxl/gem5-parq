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

import functools
import json
import os
import re
import sys
import tempfile
from pathlib import Path

MAIN_ROLES = {"pi", "architect"}
WT_ROLES = {"researcher", "experimenter", "implementor", "debugger"}
ROLES = MAIN_ROLES | WT_ROLES

# 保留核（/proc/cmdline 的 isolcpus=）只有实验员可以碰；重核任务占用它们会污染
# 正在跑的 A/B 计时。**数值不在本文件里**——单点定义在 util/roles/reserved-cores，
# 这里只负责读它（决策 0004）。读不到就 ask，不回落到内置默认：一个静默失效的
# 保留核门比没有门更危险。
CORES_CONF = "util/roles/reserved-cores"

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
    # Agent 指令文件与仓库级配置：不属于任何一次研究，归角色体系的 owner。
    ("AGENTS.md", {"architect"}),
    ("QWEN.md", {"architect"}),
    (".qwen/", {"architect"}),
    (".gitignore", {"architect"}),
    (".pre-commit-config.yaml", {"architect"}),
    (".clang-format", {"architect"}),
    ("pyproject.toml", {"architect"}),
    ("src/", set()),
    ("configs/", set()),
    ("build_opts/", set()),
    ("tests/", set()),
    ("SConstruct", set()),
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
    # 同上：仓库级配置只在主树改，分支里改会随 --no-ff 合并把它带进主干。
    ("AGENTS.md", set()),
    ("QWEN.md", set()),
    (".qwen/", set()),
    (".gitignore", set()),
    (".pre-commit-config.yaml", set()),
    (".clang-format", set()),
    ("pyproject.toml", set()),
    ("docs/refs/scripts/", {"implementor"}),
    ("src/", {"implementor", "debugger"}),
    ("configs/", {"implementor"}),
    ("build_opts/", {"implementor"}),
    ("tests/", {"implementor", "debugger"}),
    # 构建系统是代码，不是仓库配置——改它和改 src/ 同一性质。
    ("SConstruct", {"implementor"}),
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

# 造分支/worktree 在哪棵树上都只有 PI 能做（生命周期第 3 步）。
PI_ONLY_GIT = re.compile(
    r"^git\s+(?:worktree\s+add\b|checkout\s+-b\b|switch\s+-c\b"
    r"|branch\s+(?!-[avlr]|--list|--show|--contains|-d\b|-D\b))"
)

# 合并/变基**按树**判定：在主树上动的是研究主干（结题动作，PI 专属）；
# 在 worktree 里把 main 拉进分支只是分支卫生，是该分支任何角色的日常。
MERGE_LIKE = re.compile(r"^git\s+(?:merge|rebase)\b")

# 只读命令：路径或参数里出现 `scons`/`gem5.opt` 只是**提到**，不是执行。
# `ls -l build/X86/gem5.opt`、`grep -rn scons docs/`、`du -sh .../gem5.opt` 以前
# 全被判成「主树跑实验」。引号能让它们逃掉（`find -name 'gem5.opt'` 就逃掉了），
# 于是同一件事写法不同结论不同——那不是纪律，是抽签。
READONLY_CMD = re.compile(
    r"^(?:ls|stat|file|du|df|find|readlink|realpath|basename|dirname|wc|head|tail"
    r"|cat|md5sum|sha\d+sum|cmp|diff|grep|rg|nl|sort|uniq|column|test|\["
    r"|git\s+(?:log|show|diff|status|grep|ls-files|cat-file|blame|worktree\s+list))\b"
)

SCONS = re.compile(r"\bscons\b")
# `-j` 后面跟什么都算请求并行：`-j80`、`-j 80`、`-j$(nproc)`、以及裸 `-j`（make
# 的裸 -j 是"不限并发"）。旧版只匹配 `-j\d+`，`scons -j$(nproc)` 直接漏过去——
# nproc 在容器内返回 112，正好吃满含保留核的整机（S-010 §12 记过这次事故）。
JOB_J = re.compile(r"(?:^|\s)-j(?:\s*\S+)?")

# 会自己吃满整机的工具：命令位正则 + 判定"是否请求了并行"的旗标正则（None =
# 该工具默认就并行）。旗标必须逐工具绑定：xargs 的 `-n` 是批大小不是并发度，
# 一律用同一个宽正则会误杀。
JOB_SPECS: list[tuple[re.Pattern[str], re.Pattern[str] | None]] = [
    (re.compile(r"^scons\b"), JOB_J),
    (re.compile(r"^make\b"), JOB_J),
    (re.compile(r"^ninja\b"), None),
    (re.compile(r"^xargs\b"), re.compile(r"(?:^|\s)-P\s*(?!1\b)\d+")),
    (re.compile(r"^(?:py\.)?pytest\b"), re.compile(r"(?:^|\s)-n\s*(?:\d+|auto)")),
    (re.compile(r"^(?:python3?\s+)?(?:\./|tests/)*main\.py\b"), JOB_J),
]
PINNED = re.compile(r"^(?:taskset|numactl)\b")
GEM5_BIN = re.compile(r"\bgem5\.(?:opt|debug|fast|prof|perf)\b")
GEM5_OUTDIR = re.compile(r"(?:^|\s)(?:-d|--outdir)(?:[=\s]+)(\S+)")
CPU_LIST = re.compile(r"(?:taskset\s+(?:-c|--cpu-list)|numactl\s+(?:-C|--physcpubind=?))\s*=?\s*([0-9,\-]+)")

# 解释器 + 内联代码（`-c`、`-e`、或裸 `-` 接 heredoc）。门看不进代码：路径不在
# 命令位上，PATH_WRITERS/REDIRECTS 都提取不到，heredoc 正文还会被整段剥掉。
# 这不是理论漏洞——本仓库 2026-07-22 就靠一个断言才挡下一次跨树写 INDEX.md。
INLINE_CODE = re.compile(
    r"^(?:python3?|perl|ruby|node|bash|sh|zsh|awk)\b[^|;&]*?(?:\s-(?:c|e)\b|\s-\s*$)"
)
# 代码里长得像路径的东西：引号串里含 `/` 的，或裸 token 含 `/` 且不是 URL。
CODE_PATH = re.compile(r"""['"]([^'"\s]*/[^'"\s]*)['"]|(?<![\w:.-])((?:[\w.~-]+/)+[\w.~-]+)""")

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


@functools.lru_cache(maxsize=8)
def load_cores(root_s: str) -> dict[str, str] | None:
    """读 `util/roles/reserved-cores`。读不到或缺键返回 None——调用方据此 ask。"""
    try:
        text = (Path(root_s) / CORES_CONF).read_text(encoding="utf-8")
    except OSError:
        return None
    conf: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, _, v = line.partition("=")
            conf[k.strip()] = v.strip().strip("\"'")
    need = {"SERIAL_ARM_CPUS", "PARALLEL_ARM_CPUS", "BUILD_CPUS"}
    return conf if need <= conf.keys() else None


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


def check_path(
    raw: str, root: Path, role: str, tree: str, base: Path | None = None
) -> tuple[str, str] | None:
    """`raw` 若是当前角色不该写的目标，返回 (decision, reason)。

    相对路径按 `base`（harness 报的真实 shell cwd）解析，而不是按 `root`。Bash
    工具的 cwd 在调用之间**是粘的**：一次 `cd` 进别的 worktree 之后，`sed -i
    docs/specs/INDEX.md` 打的是那棵树的文件，而按 root 解析会把它错判成主树的、
    从而对 PI 放行。2026-07-22 就差点这样跨树写掉一次 INDEX.md（决策 0006）。

    走的是**词法**归一（normpath），不是 resolve()——resolve 会跟着 build/ 的 tmpfs
    软链跑出仓库，把实验员自己的活判成越界。代价是仓库内种下的软链能绕过检查，
    这正是模块 docstring 里划的护栏/沙箱那条线。
    """
    raw = os.path.expanduser(raw.strip("\"'"))
    root_s = str(root)
    target = os.path.normpath(
        raw if os.path.isabs(raw) else os.path.join(str(base or root), raw)
    )

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

    切分前先把引号串整体遮蔽成占位符：`git commit -m "…"` 的多行消息里含有
    换行、`&&`、`;`，直接按分隔符切会把消息后半段变成一个独立的「命令段」，
    于是一条只是在描述红线的提交信息被判成执行红线。这个 bug 拦下了本文件
    自己的两次提交。
    """
    stash: list[str] = []

    def mask(m: re.Match[str]) -> str:
        stash.append(m.group(0))
        return f"\x00{len(stash) - 1}\x00"

    masked = QUOTED.sub(mask, HEREDOC.sub("", cmd))
    unmask = lambda s: re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], s)  # noqa: E731

    out = []
    for raw in SEGMENT_SPLIT.split(masked):
        seg = ENV_PREFIX.sub("", unmask(raw).strip())
        if seg:
            out.append((seg, WRAPPERS.sub("", seg)))
    return out


# 反斜杠转义的引号必须参与配对，否则 `-m "... \"x\" ..."` 会在第一个 \" 处提前
# 收尾，把消息的后半段当成命令位暴露出来——这条正则的第一版就栽在这里。
QUOTED = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def unquoted(seg: str) -> str:
    """抹掉引号内的字符串再做纪律匹配。

    纪律检查（scons / gem5 / 绑核 / git 动作）用的是 search 而不是命令位锚定，
    所以 `git commit -m "... scons -j80 ..."`、`grep -rn "gem5.opt" docs/` 这类
    **只是提到**红线词的调用会被误杀。抹掉引号内容即可——代价是
    `sh -c "scons -j80"` 这种把命令塞进引号的写法逃得掉，属于 docstring 里
    说明的护栏/沙箱那条线。SIGUSR 的模式本身锚在命令位，不走这里。
    """
    return QUOTED.sub(" ", seg)


def check_discipline(seg: str, root: Path, role: str, tree: str) -> tuple[str, str] | None:
    """本项目特有的实验操作纪律。"""
    seg = unquoted(seg)
    conf = load_cores(str(root))

    m = CPU_LIST.search(seg)
    parallel_job = any(
        tool.match(seg) and (flag is None or flag.search(seg)) for tool, flag in JOB_SPECS
    )

    # 保留核的数值只有一个出处；读不到就不装作知道。
    if (m or parallel_job) and conf is None:
        return (
            "ask",
            f"读不到 `{CORES_CONF}`（保留核的单点定义，决策 0004），无法判定这条命令"
            "是否会占用 A/B 计时专用核。请先确认该文件存在且含 SERIAL_ARM_CPUS / "
            "PARALLEL_ARM_CPUS / BUILD_CPUS 三个键。",
        )

    # 隔离核：只有实验员可以碰。
    if m and conf:
        reserved = expand_cpus(conf["SERIAL_ARM_CPUS"]) | expand_cpus(conf["PARALLEL_ARM_CPUS"])
        hit = expand_cpus(m.group(1)) & reserved
        if hit and role != "experimenter":
            return (
                "deny",
                f"核 {sorted(hit)} 是内核隔离的 A/B 计时专用核"
                f"（{conf['SERIAL_ARM_CPUS']} 串行臂 / {conf['PARALLEL_ARM_CPUS']} 并行臂，"
                f"见 {CORES_CONF}），只对 experimenter 开放，当前角色是 {role}。"
                f"重核任务请限制到 BUILD_CPUS（{conf['BUILD_CPUS']}）。",
            )

    # 只读命令到此为止：下面全是「执行」类纪律，提到不算执行。
    if READONLY_CMD.match(seg):
        return None

    if SCONS.search(seg):
        if tree == "main":
            return ("deny", "主树是研究主干，不是实验场：构建只在 worktree 里做（CLAUDE.md）。")
        # experimenter 按计划自己建三臂（CLAUDE.md 角色表、experimenter
        # PROTOCOL §0/§1）；researcher 只读探查，不构建（researcher PROTOCOL §4）。
        if role == "researcher":
            return (
                "deny",
                "researcher 不做构建——只读探查可以，一旦要建二进制就是实验员/实现者的活。"
                "请求 `ROLE SWITCH: experimenter — <理由>`（跑计划里的三臂）或 "
                "`ROLE SWITCH: implementor — <理由>`（改完代码自检构建）。",
            )
    # 重核任务通则：不绑核就会吃满整机，连带保留核（决策 0004 §4）。
    if parallel_job and conf and not PINNED.match(seg):
        return (
            "deny",
            f"这是会吃满整机的并行任务，不绑核就会占用保留核 "
            f"{conf['SERIAL_ARM_CPUS']} / {conf['PARALLEL_ARM_CPUS']}，污染正在跑的 A/B 计时。"
            f"必须用 taskset/numactl 限制到 BUILD_CPUS，例如 "
            f"`taskset -c {conf['BUILD_CPUS']} scons build/... -j80`（数值出处：{CORES_CONF}）。",
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
            "建分支/worktree 是 PI 的职权（CLAUDE.md 生命周期第 3 步）。"
            "请求 `ROLE SWITCH: pi — <理由>`，或由用户直接执行。",
        )

    if MERGE_LIKE.match(seg) and tree == "main" and role != "pi":
        return (
            "deny",
            "在主树上 merge/rebase 改写的是研究主干，是 PI 的结题动作"
            "（CLAUDE.md 生命周期第 5 步）。请求 `ROLE SWITCH: pi — <理由>`。"
            "把 main 拉进某个 sNNN 分支不走这条路——那要在该分支自己的 worktree 里做。",
        )

    return None


def gate_bash(
    cmd: str, root: Path, role: str, tree: str, base: Path | None = None
) -> tuple[str, str]:
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
            verdict = check_path(raw, root, role, tree, base)
            if verdict:
                return verdict

    # --- C. 解释器内联代码：门看不进代码，只能扫代码里提到的路径 ---
    if any(INLINE_CODE.match(bare) for _, bare in segs):
        for hit in CODE_PATH.finditer(cmd):
            raw = hit.group(1) or hit.group(2)
            if not raw or "://" in raw:
                continue
            verdict = check_path(raw, root, role, tree, base)
            if verdict and verdict[0] == "deny":
                return (
                    "ask",
                    f"内联代码里出现受保护路径 `{raw}`——门看不进解释器代码，"
                    f"无法分辨这是读还是写。若是写就越权了：{verdict[1]}",
                )

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
    # harness 报的真实 shell cwd（Bash 工具的 cwd 跨调用是粘的）。只要是绝对路径就
    # 用它解析相对路径——不要求它存在：门在命令**执行前**跑，而且"shell 自称在哪"
    # 正是我们要判的东西。缺失或非绝对路径才回落到 root，与旧行为一致。
    raw_cwd = payload.get("cwd")
    base = Path(raw_cwd) if raw_cwd and os.path.isabs(raw_cwd) else root

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
        emit(*gate_bash(cmd, root, role, tree, base))

    if tool in {"Edit", "Write", "NotebookEdit"}:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if not path:
            emit("ask", f"{tool} 调用没有 file_path")
        emit(*(check_path(path, root, role, tree, base) or ("allow", f"{role} 职权内")))

    emit("ask", f"role-gate 未覆盖工具 {tool!r}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # 失败安全：暴露出来，绝不静默放行
        emit("ask", f"role-gate 内部错误：{type(exc).__name__}: {exc}")
