# 0007 — 写权矩阵翻成默认拒绝；临时脚本一律落仓库外

- 状态：已采纳（2026-07-22）
- 相关：[0003 角色门三处裁定](./0003-role-gate-scons-merge-and-repo-config.md)、
  [0006 只读误杀/粘性 cwd/内联代码](./0006-role-gate-readonly-cwd-and-inline-code.md)、
  `.claude/hooks/role-gate.py`、`CLAUDE.md`「Research role workflow」、
  `docs/roles/experimenter/PROTOCOL.md`

## 1. 背景 —— 散文早就写了，机制一直没盖住

experimenter 的协议从第一版起就写着「**不**改任何代码，**也不**改驱动脚本」
（§0、§5 红线），`WT_AREAS` 也确实挡住了 `src/`、`configs/`、`tests/`、
`build_opts/`、`SConstruct`、`docs/refs/scripts/`。看上去是闭合的。

它不闭合。`area_for()` 是**白名单匹配**，匹配不到任何前缀就返回 `None`，
`check_path()` 随即返回 `None` —— 也就是 `allow`：

```python
def area_for(rel, tree):
    for prefix, owners in MAIN_AREAS if tree == "main" else WT_AREAS:
        if rel == prefix or rel.startswith(prefix):
            return prefix, owners
    return None          # ← 未列举 = 放行
```

于是**树内任何未列举路径对所有角色都是敞开的**。这不只是「临时脚本」的问题，
被漏掉的还有实打实的代码目录：`util/`（除 `util/roles/`）、`ext/`、`site_scons/`、
`system/`，以及仓库根上的任何新文件。

不是纸面推演。写这份记录时，s019-avgstor worktree（`.active-role` = experimenter）
的 `git status` 里躺着：

```
?? run-a0.sh          ← 仓库根，实验员写的，门当时给的是 allow
```

一条本该被拒的写入，被判成放行，而且没有留下任何痕迹——直到有人恰好去看
`git status`。这与 0006 §3 那次跨树写 INDEX.md 是同一类故障：**门说了 allow，
护栏形同虚设**。

## 2. 缺陷的性质：默认方向错了

白名单矩阵配上「未命中即放行」，等于把矩阵变成了黑名单——只不过是一份**伪装成
白名单**的黑名单。它的失效方式是静默的：新增一个目录、新写一个根级脚本，都不需要
任何人修改矩阵就自动获得写权。矩阵越老，漏洞越大。

`CLAUDE.md` 的角色表本身写的是白名单语义（「Writable areas」逐项列举），
所以这里是**执行副本背离了散文正本**，而不是散文没写清楚。

## 3. 决策

**A. 两张矩阵末位各加一条兜底条目，未列举路径默认拒绝。**

- `MAIN_AREAS` 末位：`(CATCHALL, set())` —— 主树的可写区在角色表里已逐项列全，
  之外一律拒绝。主树既不构建也不跑实验，没有合法的「未列举写入」。
- `WT_AREAS` 末位：`(CATCHALL, {"implementor", "debugger"})` —— 未列举路径压倒性地
  是代码相邻的（`util/`、`ext/`、`site_scons/`、根级脚本），归代码角色。
  researcher / experimenter 因此**只剩** `docs/specs/S-NNN-*.md` 一处可写，
  与角色表逐字一致。

`CATCHALL` 取空串：`rel.startswith("")` 恒真，且必须排在最后（先匹配者胜）。

**B. `build/` 必须显式列举，否则兜底会打死所有构建。**

`check_path()` 走**词法**归一而非 `resolve()`（0006 之前就定下的：resolve 会跟着
`build/` 的 tmpfs 软链跑出仓库）。代价是 `build/X86/gem5.opt` 在门看来仍是
**树内路径**——加了兜底就会被拒。因此 `WT_AREAS` 在兜底之前显式放行
`("build/", {"experimenter", "implementor", "debugger"})`（researcher 不构建）。

这条不是例外，是词法归一的直接后果，删掉它下一次构建就会撞墙。

**C. 临时脚本、中间产物一律写到 `/tmp/<...>`，不进仓库任何位置。**

`check_path()` 对 `/tmp` 早就直接放行，机制上不需要新东西；需要的是把落点写死成
纪律，并给出**唯一的例外出口**：

- 一次性的分析/驱动脚本 → `/tmp/<something>`。要留痕就把脚本正文贴进 spec
  （spec 是实验员的可写区），可执行副本留在 `/tmp`。
- 值得长期留存、会被后续臂复用的驱动脚本 → `ROLE SWITCH: implementor`，
  收进 `docs/refs/scripts/`，正常提交。

「仓库内但 gitignore 掉」这条路**不采纳**：它把两个不同的问题混在一起。gitignore
管的是「什么该入库」，写权矩阵管的是「谁能写哪里」；用前者代替后者，等于让任何
角色只要挑对目录就能在树内自由落文件，而 `git status` 从此再也不会提醒任何人。
0007 之所以能被发现，靠的正是 `?? run-a0.sh` 在 `git status` 里露了头。

## 4. 否决的备选

- **只给 experimenter 加一条「禁止写树内未列举路径」的特例。** 修得动这次的病例，
  修不动病因：同一个洞对 researcher 一样开着，对将来新增的角色也一样开着。默认
  方向错了就该改默认方向。
- **把矩阵逐条补全（列举 `util/`、`ext/`、`site_scons/`、`system/` …）。** 治标：
  下一个新目录照样漏。而且「补全」本身没有终止条件——上游 gem5 的目录会变。
- **允许树内 gitignore 掉的临时脚本。** 见 §3.C 末段。

## 5. 后果与已知代价

- **主树的 `docs/refs/**` 现在对 architect 也是拒绝的。** 角色表里本来就没给
  architect 这个区（`docs/refs/scripts/` 在 worktree 里归 implementor），
  所以这是**执行副本终于对齐了散文**，不是新限制。但它确实是一处行为变化，
  第一次撞上时容易误以为是 bug。
- 兜底的拒绝理由必须自带出路，否则会训练出「换个目录再试」的适应。
  `deny_reason()` 对兜底给专门文案：指向 `/tmp`，并指出长期脚本走 implementor。
- 门仍然只是护栏：`python3 -c` 拼路径、树内种软链都能绕过（0006 §4 已划过这条线）。
  兜底提高的是**手滑的成本**，不是不可绕过性。
- `ln` 不在 `PATH_WRITERS` 里，所以建 `build` 软链不受本次改动影响。若将来把 `ln`
  加进写入命令清单，必须同时确认 §3.B 的 `build/` 条目仍然放行，否则 PI 建 worktree
  的最后一步会被自己的门挡住。

## 6. 何时应重新审视

- 若某个角色频繁撞上兜底且理由正当（例如 implementor 需要改 `ext/` 里的第三方
  依赖），那说明该路径值得**显式列举**并写进 `CLAUDE.md` 的角色表——而不是把
  兜底放宽。兜底被放宽一次，§2 的失效模式就回来了。
- 若 `/tmp` 的临时脚本反复被重写（同一段分析每个会话写一遍），说明它其实不是临时的，
  应该走 implementor 进 `docs/refs/scripts/`。
- 若出现「兜底拒绝 → 用户确认 → 反正也放行了」的固定循环，说明拒绝文案没给对出路，
  回头改文案，不要改判决。
