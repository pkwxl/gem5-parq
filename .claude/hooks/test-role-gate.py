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
    # 保留核的单点定义（决策 0004）。用真文件而不是假值——门读不到它就一律 ask，
    # 假根若没有这份文件，全部纪律用例都会假红。
    real = Path(__file__).resolve().parents[2] / "util" / "roles" / "reserved-cores"
    for root in (main, wt):
        (root / "util" / "roles" / "reserved-cores").write_text(real.read_text())
    return main, wt


def run(root: Path, role: str, tool: str, ti: dict, cwd: str | None = None) -> tuple[str, str]:
    (root / ".active-role").write_text(role + "\n")
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(root))
    payload = {"tool_name": tool, "tool_input": ti}
    if cwd is not None:
        payload["cwd"] = cwd
    p = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
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
        # ---- 仓库级配置与 agent 指令文件（ADR 0003 缺口 3）----
        (main_root, "architect", W(".gitignore"), "allow", "仓库配置归 architect"),
        (main_root, "architect", W(".qwen/QWEN.md"), "allow", "agent 指令文件归 architect"),
        (main_root, "architect", W("pyproject.toml"), "allow", "格式化配置归 architect"),
        (main_root, "pi", W(".gitignore"), "deny", "PI 不改仓库配置"),
        (main_root, "architect", W("SConstruct"), "deny", "构建系统是代码，主树不写"),
        # ---- 主树不是实验场 ----
        (main_root, "architect", B("scons build/X86/gem5.opt -j8"), "deny", "主树禁构建"),
        (main_root, "pi", B("./build/X86/gem5.opt -d /tmp/x foo.py"), "deny", "主树禁跑 gem5"),
        # ---- PI 专属 git 动作 ----
        (main_root, "pi", B("git merge --no-ff s019-x"), "allow", "PI 可合并"),
        (main_root, "pi", B("git worktree add /workspace/gem5-wt/s019 s019"), "allow", "PI 可建 worktree"),
        (main_root, "architect", B("git merge --no-ff s019-x"), "deny", "architect 不可合并"),
        (wt_root, "implementor", B("git checkout -b s020-y"), "deny", "分支由 PI 建"),
        (main_root, "pi", B("git branch -a"), "allow", "列分支不是造分支"),
        # ---- merge/rebase 按树判定（ADR 0003 缺口 2）----
        (main_root, "architect", B("git rebase main"), "deny", "主树变基改写主干"),
        (wt_root, "experimenter", B("git merge main"), "allow", "分支自取 main 更新"),
        (wt_root, "researcher", B("git rebase main"), "allow", "rebase 到 main 是分支卫生"),
        (wt_root, "implementor", B("git worktree add /workspace/gem5-wt/z z"), "deny", "建 worktree 仍是 PI"),
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
        (wt_root, "implementor", W(".gitignore"), "deny", "仓库配置只在主树改"),
        (wt_root, "implementor", W(".qwen/QWEN.md"), "deny", "agent 指令文件只在主树改"),
        (wt_root, "implementor", W("SConstruct"), "allow", "构建系统归 implementor"),
        (wt_root, "researcher", W("SConstruct"), "deny", "researcher 不改构建系统"),
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
        # ---- 重核任务必须绑核（ADR 0004 §4）----
        (wt_root, "implementor", B("scons build/X86/gem5.opt -j80"), "deny", "未绑核的 -j 吃满整机"),
        (wt_root, "implementor", B("scons build/X86/gem5.opt"), "allow", "无 -j 不设限"),
        (wt_root, "implementor", B("scons build/X86/gem5.opt -j$(nproc)"),
         "deny", "-j$(nproc) 旧版漏网，nproc 在容器内是 112"),
        (wt_root, "implementor", B("taskset -c 0-53,56-91 scons build/X86/gem5.opt -j$(nproc)"),
         "allow", "绑到 BUILD_CPUS 后 -j$(nproc) 也放行"),
        (wt_root, "implementor", B("make -j"), "deny", "make 裸 -j 是不限并发"),
        (wt_root, "implementor", B("ninja -C build"), "deny", "ninja 默认就吃满整机"),
        (wt_root, "implementor", B("taskset -c 0-53 ninja -C build"), "allow", "绑核后放行"),
        (wt_root, "experimenter", B("xargs -P 8 -I{} sh -c 'echo {}' < list"),
         "deny", "xargs -P 是并发度"),
        (wt_root, "experimenter", B("xargs -n 10 echo < list"), "allow", "xargs -n 是批大小不是并发"),
        (wt_root, "implementor", B("pytest -n auto tests/"), "deny", "pytest -n auto"),
        (wt_root, "implementor", B("pytest tests/pyunit"), "allow", "串行 pytest 不设限"),
        (wt_root, "experimenter", B("cd tests && ./main.py run -j6"), "deny", "回归测试的 -j 同理"),
        (wt_root, "experimenter", B("taskset -c 0-53 scons build/X86/gem5.opt -j40"),
         "allow", "实验员自己建三臂（ADR 0003 缺口 1）"),
        (wt_root, "researcher", B("taskset -c 0-53 scons build/X86/gem5.opt -j40"),
         "deny", "researcher 仍不构建"),
        (wt_root, "debugger", B("scons build/X86/gem5.debug"), "allow", "debugger 可重建"),
        # ---- 只读命令只是「提到」红线词，不是执行（ADR 0006 §2）----
        (main_root, "architect", B("ls -l build/X86/gem5.opt"), "allow", "ls 不是跑 gem5"),
        (main_root, "architect", B("du -sh build/X86/gem5.opt"), "allow", "du 同理"),
        (main_root, "architect", B("grep -rn scons docs/"), "allow", "grep 不是构建"),
        (main_root, "architect", B("git log --oneline -- build/X86/gem5.opt"), "allow", "git log 只读"),
        (main_root, "pi", B("./build/X86/gem5.opt -d /tmp/x f.py"), "deny", "真跑仍拦"),
        (main_root, "architect", B("scons build/X86/gem5.opt"), "deny", "真构建仍拦"),
        (wt_root, "researcher", B("stat build/X86/gem5.opt"), "allow", "researcher 可只读探查二进制"),
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

    # 单点定义缺失时的失败安全方向：ask，绝不静默放行（ADR 0004 §3）。
    conf = wt_root / "util" / "roles" / "reserved-cores"
    saved = conf.read_text()
    conf.unlink()
    extra = [
        ("experimenter", B("taskset -c 54,55 ./build/X86/gem5.opt -d /tmp/r f.py"),
         "ask", "读不到保留核清单就不装作知道"),
        ("implementor", B("scons build/X86/gem5.opt -j80"), "ask", "同上，重核任务也降级为 ask"),
        ("implementor", B("git status"), "allow", "与保留核无关的命令不受影响"),
    ]
    for role, (tool, ti), want, label in extra:
        got, why = run(wt_root, role, tool, ti)
        if got != want:
            fails += 1
            print(f"FAIL  [{role}@wt/无配置] {label}\n      期望 {want} 实得 {got}: {why}")
    conf.write_text(saved)

    # 粘性 cwd 与解释器内联代码（ADR 0006 §3/§4）。Bash 的 cwd 跨调用不重置，
    # 一次 cd 进别的树之后，相对路径打的是那棵树的文件。
    other_wt = "/workspace/gem5-wt/some-other-branch"
    cwd_cases = [
        (main_root, "pi", B("sed -i s/a/b/ docs/specs/INDEX.md"), str(main_root),
         "allow", "cwd 就是本树时行为不变"),
        (main_root, "pi", B("sed -i s/a/b/ docs/specs/INDEX.md"), other_wt,
         "deny", "cd 进别的 worktree 后相对路径按那棵树算"),
        (main_root, "architect", B("python3 -c \"open('docs/specs/INDEX.md','w')\""), other_wt,
         "ask", "内联代码里的跨树路径降级为 ask"),
        (main_root, "architect", B("python3 -c \"print(1)\""), None,
         "allow", "内联代码没提受保护路径就不打扰"),
        (main_root, "architect", B("python3 util/roles/gen.py"), None,
         "allow", "跑脚本文件不是内联代码"),
        (wt_root, "implementor", B("python3 - <<'EOF'\nopen('/workspace/gem5/CLAUDE.md','w')\nEOF"),
         None, "ask", "heredoc 正文里的主树路径同样降级"),
    ]
    for root, role, (tool, ti), cwd, want, label in cwd_cases:
        got, why = run(root, role, tool, ti, cwd)
        if got != want:
            fails += 1
            print(f"FAIL  [{role}@{root.name} cwd={cwd}] {label}\n      期望 {want} 实得 {got}: {why}")

    # 配置自身的自洽：BUILD_CPUS 与两条臂必须互斥，否则"绑到 BUILD_CPUS"这条
    # 建议本身就会踩保留核。没有任何代码强制这三者的关系，只能在这里断言。
    conf_vals: dict[str, str] = {}
    for line in (Path(__file__).resolve().parents[2] / "util" / "roles" / "reserved-cores") \
            .read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, _, v = line.partition("=")
            conf_vals[k.strip()] = v.strip()

    def cpus(spec: str) -> set[int]:
        out: set[int] = set()
        for part in spec.split(","):
            if "-" in part:
                lo, _, hi = part.partition("-")
                out |= set(range(int(lo), int(hi) + 1))
            elif part.strip():
                out.add(int(part))
        return out

    serial, parallel = cpus(conf_vals["SERIAL_ARM_CPUS"]), cpus(conf_vals["PARALLEL_ARM_CPUS"])
    build = cpus(conf_vals["BUILD_CPUS"])
    conf_checks = [
        (not (serial & parallel), "两条臂的核不得重叠"),
        (not (build & (serial | parallel)), "BUILD_CPUS 不得含保留核"),
        (bool(serial) and bool(parallel) and bool(build), "三个键都不得为空"),
    ]
    for ok, label in conf_checks:
        if not ok:
            fails += 1
            print(f"FAIL  [reserved-cores] {label}")

    total = len(cases) + len(extra) + len(cwd_cases) + len(conf_checks)
    print(f"\n{total - fails}/{total} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
