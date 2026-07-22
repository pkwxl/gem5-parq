#!/usr/bin/env python3
"""role-gate.py 的回归测试。用法：python3 .claude/hooks/test-role-gate.py

在 $HOME/.cache/gem5-role-gate-test/ 下造两个假仓库根——一个 `.git` 是目录（主树），
一个 `.git` 是文件（worktree）——然后用伪造的 PreToolUse payload 驱动钩子，
断言 allow/deny/ask。假根**不能**放在 /tmp 下：check_path 对 /tmp 直接放行，
放那里会让所有用例都假绿。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).with_name("role-gate.py")
FIXTURE = Path(os.path.expanduser("~/.cache/gem5-role-gate-test"))


def setup() -> tuple[Path, Path]:
    main, wt = FIXTURE / "main", FIXTURE / "wt"
    for root in (main, wt):
        for d in ("docs/specs", "docs/roles", "docs/decisions", "docs/roadmap",
                  "docs/refs/scripts", "src/sim", "configs", "tests", ".claude", "util/roles"):
            (root / d).mkdir(parents=True, exist_ok=True)
    (main / ".git").mkdir(exist_ok=True)
    (wt / ".git").write_text("gitdir: /workspace/gem5/.git/worktrees/wt\n")
    return main, wt


def run(root: Path, role: str, tool: str, ti: dict) -> tuple[str, str]:
    (root / ".active-role").write_text(role + "\n")
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(root))
    p = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": ti}),
        capture_output=True, text=True, env=env,
    )
    if p.returncode != 0 or not p.stdout:
        return ("CRASH", p.stderr.strip()[-300:])
    out = json.loads(p.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def main() -> int:
    main_root, wt_root = setup()
    B = lambda cmd: ("Bash", {"command": cmd})          # noqa: E731
    W = lambda p: ("Write", {"file_path": p})           # noqa: E731

    cases: list[tuple[Path, str, tuple[str, dict], str, str]] = [
        # ---- 主树写权矩阵 ----
        (main_root, "architect", W("CLAUDE.md"), "allow", "architect 拥有 CLAUDE.md"),
        (main_root, "architect", W("docs/decisions/0002-x.md"), "allow", "architect 拥有决策记录"),
        (main_root, "architect", W("docs/specs/S-019-x.md"), "allow", "architect 写 spec 初版"),
        (main_root, "architect", W("docs/specs/INDEX.md"), "deny", "INDEX 是 PI 专属"),
        (main_root, "architect", W("docs/roadmap/ROADMAP.md"), "deny", "roadmap 是 PI 专属"),
        (main_root, "architect", W("src/sim/eventq.cc"), "deny", "主树不写代码"),
        (main_root, "pi", W("docs/specs/INDEX.md"), "allow", "PI 拥有 INDEX"),
        (main_root, "pi", W("docs/roadmap/ROADMAP.md"), "allow", "PI 拥有 roadmap"),
        (main_root, "pi", W("docs/specs/S-019-x.md"), "deny", "spec 正文是 architect 的"),
        (main_root, "pi", W("CLAUDE.md"), "deny", "CLAUDE.md 是 architect 的"),
        # ---- 主树不是实验场 ----
        (main_root, "architect", B("scons build/X86/gem5.opt -j8"), "deny", "主树禁构建"),
        (main_root, "pi", B("./build/X86/gem5.opt -d /tmp/x foo.py"), "deny", "主树禁跑 gem5"),
        # ---- PI 专属 git 动作 ----
        (main_root, "pi", B("git merge --no-ff s019-x"), "allow", "PI 可合并"),
        (main_root, "pi", B("git worktree add /workspace/gem5-wt/s019 s019"), "allow", "PI 可建 worktree"),
        (main_root, "architect", B("git merge --no-ff s019-x"), "deny", "architect 不可合并"),
        (wt_root, "implementor", B("git checkout -b s020-y"), "deny", "分支由 PI 建"),
        (main_root, "pi", B("git branch -a"), "allow", "列分支不是造分支"),
        # ---- worktree 写权矩阵 ----
        (wt_root, "implementor", W("src/sim/eventq.cc"), "allow", "implementor 拥有 src"),
        (wt_root, "implementor", W("docs/refs/scripts/drv.py"), "allow", "驱动脚本归 implementor"),
        (wt_root, "implementor", W("docs/specs/INDEX.md"), "deny", "worktree 不碰 INDEX"),
        (wt_root, "implementor", W("CLAUDE.md"), "deny", "worktree 不碰 CLAUDE.md"),
        (wt_root, "debugger", W("src/mem/ruby/x.cc"), "allow", "debugger 可最小改 src"),
        (wt_root, "debugger", W("configs/example/x.py"), "deny", "configs 归 implementor"),
        (wt_root, "researcher", W("docs/specs/S-019-x.md"), "allow", "researcher 写 spec"),
        (wt_root, "researcher", W("src/sim/eventq.cc"), "deny", "researcher 不改代码"),
        (wt_root, "experimenter", W("docs/specs/S-019-x.md"), "allow", "实验员写结果"),
        (wt_root, "experimenter", W("docs/refs/scripts/drv.py"), "deny", "实验员不改驱动脚本"),
        # ---- 跨树写入 ----
        (wt_root, "implementor", W("/workspace/gem5/src/sim/eventq.cc"), "deny", "不得跨树写主树"),
        (wt_root, "implementor", W("/workspace/gem5-wt/other/src/x.cc"), "deny", "不得跨树写别的 worktree"),
        (wt_root, "experimenter", W("/tmp/gem5-exp/run.log"), "allow", "/tmp 是实验产物区"),
        (wt_root, "experimenter", W("/workspace/shm/gem5/wt/build/x"), "allow", "tmpfs build 区"),
        # ---- 角色与树不匹配 ----
        (main_root, "implementor", W("src/x.cc"), "ask", "worktree 角色出现在主树"),
        (wt_root, "pi", W("docs/specs/INDEX.md"), "ask", "主树角色出现在 worktree"),
        # ---- 隔离核纪律 ----
        (wt_root, "experimenter", B("taskset -c 92-111 ./build/X86/gem5.opt -d /tmp/r foo.py"),
         "allow", "实验员可用并行臂保留核"),
        (wt_root, "experimenter", B("taskset -c 54,55 ./build/X86/gem5.opt -d /tmp/r foo.py"),
         "allow", "实验员可用串行臂保留核"),
        (wt_root, "implementor", B("taskset -c 92-95 scons build/X86/gem5.opt -j4"),
         "deny", "implementor 不得占保留核"),
        (wt_root, "implementor", B("numactl -C 54-60 ./build/X86/gem5.opt -d /tmp/r foo.py"),
         "deny", "numactl 形式也要拦"),
        (wt_root, "implementor", B("taskset -c 0-53,56-91 scons build/X86/gem5.opt -j80"),
         "allow", "限制到非保留核就放行"),
        # ---- scons -j 纪律 ----
        (wt_root, "implementor", B("scons build/X86/gem5.opt -j80"), "deny", "未绑核的 -j 吃满整机"),
        (wt_root, "implementor", B("scons build/X86/gem5.opt"), "allow", "无 -j 不设限"),
        (wt_root, "experimenter", B("taskset -c 0-53 scons build/X86/gem5.opt -j40"),
         "deny", "实验员不做构建"),
        # ---- gem5 输出目录纪律 ----
        (wt_root, "experimenter", B("./build/X86/gem5.opt fs.py"), "deny", "缺 -d 会写进仓库"),
        (wt_root, "experimenter", B("./build/X86/gem5.opt -d m5out fs.py"), "deny", "-d 指到仓库内"),
        (wt_root, "experimenter", B("./build/X86/gem5.opt --outdir=/tmp/r fs.py"), "allow", "-d 在仓库外"),
        (wt_root, "experimenter", B("./build/X86/gem5.opt --help"), "allow", "--help 不是实验"),
        (wt_root, "researcher", B("./build/X86/gem5.opt -d /tmp/r fs.py"), "deny", "researcher 不跑实验"),
        # ---- SIGUSR1/2 ----
        (wt_root, "experimenter", B("kill -USR1 12345"), "deny", "禁 SIGUSR1"),
        (wt_root, "experimenter", B("kill -s SIGUSR2 12345"), "deny", "禁 SIGUSR2（-s 形式）"),
        (wt_root, "experimenter", B("kill -10 12345"), "deny", "禁 SIGUSR1（数字形式）"),
        (wt_root, "experimenter", B("pkill -USR1 gem5.opt"), "deny", "pkill 形式"),
        (wt_root, "experimenter", B("kill -9 12345"), "allow", "SIGKILL 不在红线内"),
        (wt_root, "researcher", B("grep -rn 'kill -USR1' docs/"), "allow", "只是提到红线词"),
        # ---- 自提权 ----
        (wt_root, "implementor", B("chmod +w docs/specs/INDEX.md"), "deny", "手工 chmod 提权"),
        (wt_root, "implementor", B("echo pi > .active-role"), "deny", "自选角色"),
        (wt_root, "implementor", B("sed -i s/a/b/ CLAUDE.md"), "deny", "sed -i 绕 chmod"),
        (wt_root, "implementor", B("rm docs/specs/INDEX.md"), "deny", "rm+重建绕 chmod"),
        (wt_root, "implementor", B("cp /tmp/x.md docs/specs/INDEX.md"), "deny", "cp 目标是保护区"),
        (wt_root, "implementor", B("cp docs/specs/INDEX.md /tmp/x.md"), "allow", "读保护区是自由的"),
        # ---- 外向 / 不可逆 ----
        (wt_root, "implementor", B("git push origin main"), "deny", "推送由用户手工做"),
        (wt_root, "implementor", B("sudo apt install m4"), "ask", "sudo 交人确认"),
        (wt_root, "implementor", B("git commit --amend -m x"), "ask", "改写历史"),
        (wt_root, "implementor", B("git reset --hard HEAD~1"), "ask", "丢弃工作"),
        # ---- 只是「提到」红线词，不是执行它（引号内容不参与纪律匹配）----
        (wt_root, "implementor", B("git commit -m 'build: scons -j80 needs taskset now'"),
         "allow", "提交信息里提到 scons -j"),
        (wt_root, "researcher", B("grep -rn 'gem5.opt -d m5out' docs/"), "allow", "grep 模式里提到 gem5"),
        (wt_root, "implementor", B("git commit -m 'pin to taskset -c 92-111'"),
         "allow", "提交信息里提到保留核"),
        (wt_root, "implementor", B("echo 'git merge --no-ff is pi-only'"), "allow", "引号内的 git merge"),
        (main_root, "architect", B('git commit -m "note: sh -c \\"scons -j80\\" escapes the gate"'),
         "allow", "转义引号必须参与配对，否则后半段会露出来"),
        (main_root, "architect",
         B('git commit -m "hdr" -m "line one\nmentions scons -j80\nand taskset -c 92-111\n"'),
         "allow", "引号内的换行不得把消息切成命令段"),
        (main_root, "architect", B('git commit -m "a && scons -j80 ; kill -USR1 1"'),
         "allow", "引号内的 && ; 同理"),
        (wt_root, "implementor", B("make && scons build/X86/gem5.opt -j80"),
         "deny", "引号外的 && 仍要切分并拦截"),
        # ---- 日常放行 ----
        (wt_root, "implementor", B("git commit -m 'mem: S-019 T1 -- x'"), "allow", "正常提交"),
        (wt_root, "researcher", B("grep -rn curTick src/mem/ruby | head -50"), "allow", "只读探查"),
        (wt_root, "implementor", B("cd /workspace && ls && git status"), "allow", "多段只读"),
        (wt_root, "experimenter", B("./build/X86/gem5.opt -d /tmp/a f.py > /tmp/a/log 2>&1"),
         "allow", "重定向到 /tmp"),
    ]

    fails = 0
    for root, role, (tool, ti), want, label in cases:
        got, why = run(root, role, tool, ti)
        if got != want:
            fails += 1
            print(f"FAIL  [{role}@{root.name}] {label}\n      期望 {want} 实得 {got}: {why}")
    print(f"\n{len(cases) - fails}/{len(cases)} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
