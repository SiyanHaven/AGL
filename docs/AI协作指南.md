# AGL 编译器 · AI 协作指南（报错 / 优化建议专用）

> 这份文档给**另一个 AI 协作方**看：你不需要先读完整代码库，按这份指南就能
> ① 把编译器跑起来、② 读懂报错并复现、③ 提出靠谱的优化/改进建议。
> 最后更新：2026-10-28，对应代码状态：阶段 1 全链路 + 阶段 2 全工作包 + 阶段 3 导入器第一步 + 增广赋值。

---

## 0. 一句话定位

AGL（Agent Grounded Language）是一门**给 AI Agent 用、也给人读**的编程语言：
中文 / 英文双分支同构（同一套 IR，关键字可中英切换），编译器输出 Python 并**保证行号对齐**
（AGL 第 N 行 == 产物 Python 第 N 行），所有报错回译成 AGL 行号 + 中文原因。
零第三方依赖，纯标准库实现。

---

## 1. 环境与前置

- **只用 WorkBuddy 自带的 Python**，不要假设系统装了 Python。
- 仓库根目录（下文记为 `<AGL 目录>`）下直接跑命令；不需要安装、不需要虚拟环境。
- **零第三方依赖**是铁律：任何新增代码不得 `import` 外部包（标准库除外）。

---

## 2. 必会命令（字字可复现）

在 `<AGL 目录>` 中执行：

```bash
# 一致性体检：28 项检查（规格 + 黄金用例 + 中英一致性 + 源码规范 + 韧性 + AGB 产物）
python -m compiler.cli doctor
python -m tests.suite            # 同一套引擎，可独立跑

# 单项编译 / 运行 / 检查
python -m compiler.cli run   <文件.agl>          # 编译并执行（默认 VM），错误回译到 AGL 行号
python -m compiler.cli run   <文件.agb>          # 直接执行 AGB 字节码
python -m compiler.cli build <文件.agl> -o <目录> # 产出 <文件>.agb 和 <文件>.agl.ir（不再产出 .py）
python -m compiler.cli check <文件.agl>          # 词法/语法/语义/IR 检查，不产物
python -m compiler.cli ir    <文件.agl>          # 打印 IR
python -m compiler.cli tokens <文件.agl>         # 打印 Token 流

# 中英双分支
python -m compiler.cli convert <文件.agl>        # 中<->英关键字互翻（只翻关键字，标识符原样）
python -m compiler.cli roundtrip <文件.agl>      # 往返校验：翻过去必须能逐字翻回来
python -m compiler.cli compare A.agl B.agl      # 结构等价（忽略标识符）

# Python -> AGL 导入器（阶段 3）
python -m compiler.cli import <文件.py>          # 打印 AGL 源码；翻不了的会逐条说明原因
python -m compiler.cli import <文件.py> -o <文件.agl>

# 其它
python -m compiler.cli card --lang zh|en         # 关键字速查卡（写码前先读这一页）
python -m compiler.cli ffi  <文件.agl>           # 列出用了哪些外部库、各自什么效应
python -m compiler.cli grammar --dir editor/vscode  # 由关键字表重新生成编辑器语法高亮

# 黄金产物刷新（仅在「有意改动」产物后使用，见 §6）
python -m compiler.cli doctor --update-golden
```

**递交任何改动前，先跑 `python -m tests.suite`，必须 28/28 全绿。**

---

## 3. 项目结构（关键目录 → 职责）

```
<AGL 目录>
├─ compiler/
│  ├─ lexer/       词法器：符号表、全角归一、_SYMBOLS 长度降序匹配
│  ├─ parser/      语法器：语句/表达式 -> IR（Stmt*/Expr* 字典树）
│  ├─ checker/     语义/类型检查（作用域、效应、常量不可重赋）
│  ├─ ir/          opt.py = IR 优化 Pass（折叠/死代码/CSE/化简/无用变量）
│  ├─ backend_py/  后端：IR -> Python 产物（行号对齐核心）
│  ├─ importer.py  Python 源码导入器（AST -> AGL 源码，翻不了就直说）
│  ├─ cli/         aglc 命令行入口 + doctor.py 薄壳
│  ├─ mcp.py/lsp.py 给 AI 客户端/编辑器连的服务器
│  └─ card.py/grammar.py/diagnostics.py 速查卡/语法生成/错误渲染
├─ spec/
│  ├─ keywords.toml  唯一关键字来源（[[builtin]] 表、[[op]]、[[attr]]）
│  └─ ir-schema.json IR 契约（additionalProperties:false，扩字段必须同步改）
├─ tests/
│  ├─ golden/     黄金用例（*.agl + 期望产物 *.py / *.agl.ir，逐字比对）
│  ├─ import/     导入器用例（ok_*.py 能导 / blocked_*.py 必须被拒）
│  └─ suite/      一致性测试套件（checks.py 放检查，__init__.py 编排）
├─ stdlib/         AGL 标准库（*.agl 标准库模块）
├─ editor/vscode/  编辑器扩展（语法高亮由 grammar 命令生成，勿手改）
└─ docs/          标准与契约.md（规格+30 条硬规则+变更记录）、阶段2拆解.md、本指南
```

**跨文件铁律**：关键字/内建/运算符/属性**只认 `spec/keywords.toml` 一张表**，
词法、语义、后端、转换器、速查卡、语法高亮统一读它；改语法先改表，再重生语法。

---

## 4. 四条铁律（破了就是回归，务必先读）

1. **行号对齐**：AGL 第 N 行 = 后端产物第 N 行。`doctor` 第 9 项把它钉成 **100% 硬门槛**
   （掉一条就整体变红）。任何优化都**不得插入新语句**（插入会让后面行号整体顺延）。
   唯一已知的 1 行边界：单行回调块 `当任务完成(甲) { 打印(...) }` 后**紧跟复合语句**
   （`如果`/`循环`）时会顺延 1 行 —— 这是 Python 语法边界，不藏着。
2. **中英同构**：新语法两分支同时进表、同时进 IR、同时有用例。双分支骨架比对靠
   `compiler/convert.py` 的 `_IDENT_FIELDS` —— **新增的用户命名字段要加进去**。
   英文类型关键字是 `String`（不是 `Text`）、`Int`、`Float`、`Bool`。
3. **零第三方依赖**：编译器 / LSP / MCP / 标准库都不许引外部包。
4. **优化必须 opt-in 且过对拍**：任何"跑得更快"的改动都要先证明"算得一样"
   （`doctor` 第 15 项逐 Pass 校验"至少真的生效一次"）。

**附：最易踩的坑（已踩过 3 次）** —— 遍历 IR 表达式/语句**必须走统一入口**。
表达式用 `compiler/ir/opt.py` 的 `_children(e)`（或各 Pass 里的统一递归）；
语句侧的引用收集看 `_collect_refs` 的 key 清单（`value`/`target`/`cond`/`iter`/`task`）。
改 IR 或加 statement 类型时，**第一件事是回头核对这两个清单**，否则症状是
变量被误删 / `NameError` / 静默给出错误答案。

---

## 5. 错误码与报错体系（怎么读、怎么复现）

- 报错格式：`AGL-E-NNNN  <文件> 第 N 行` + 源码片段 + 中文问题 + 建议 + （可选）`原因：`栏。
- **`原因：`栏是唯一允许保留宿主原始异常的地方**（留给排查的人）；正文主体必须是 AGL 名词、
  不得泄漏 `.py` 文件路径、`Traceback`、`asyncio` 等宿主术语（`doctor` 第 22 项守这道线）。
- 运行期错误同样回译：行号指回 AGL 源码行，回调里的错也不许吞（`doctor` 第 19 项）。
- **复现 bug 的标准动作**：先回退你的修复让它报错/错值给你看，再修，再复测。
  代理报告和直觉都可能有假阳性——本仓库的每条修复都走这个流程。

---

## 6. 一致性测试怎么加（28 项不是天花板）

- `tests/suite/checks.py` 放具体检查，`tests/suite/__init__.py` 的 `all_checks()` 编排。
- 黄金产物比对：`tests/golden/*.agl` 编译出的 `.py`/`.agl.ir` 必须与期望文件**逐字一致**。
  有意改动产物后跑 `python -m compiler.cli doctor --update-golden` 刷新；
  **生成物（editor 语法文件、tests/golden/*.py|*.ir）不许手改**，改动是有意的就跑刷新命令。
- 28 项目前覆盖：规格文件 / 关键字表 / 中英双分支 / 黄金编译运行 / 往返校验 /
  语法高亮 / 标准库 / IR 优化 / 内建 / 行号对齐 / LSP / 异步运行时 / FFI / 源码规范 /
  错误纯洁性 / 编译器韧性 / Python 导入器（翻译+逐字对拍）/ VM 效应权限白名单 /
  AGB 产物与默认 VM（build 不出 .py、run 默认 VM、超集显式报错）。

---

## 7. 当前已落地的语言特性（速览，便于你接手）

- 变量/常量（`变量`/`常量`，常量不可重赋）、函数（同步/异步）、类型注解。
- 控制流：`如果/否则`、`当`（while）、`循环 每 在`（for-in）、`尝试/捕获`、`中断`/`继续`、`返回`。
- **增广赋值 `+= -= *= /= %=`**（2026-10-28 新增，本轮主线）：左值须是变量或成员/下标，
  常量不可增广；IR 新节点 `StmtAugAssign`；导入器从退化展开改为直出原生 `x += 1`。
- 容器：列表 `[...]`、映射表 `{...}`、下标 `x[0]`、成员 `x.长度`、切片 `切片()`。
- 字符串：拼接 `+`、切分/拼接/替换/去空白/大小写/包含/转文本。
- 异步：`异步`/`任务`/`等待`/`当收到进度`/`当任务完成`/`当任务失败`/`取消`（单线程事件循环）。
- 效应系统：FFI 黑盒调用（`导入 外部 X [效应 …]`），外部库一律最坏效应，缺声明编译期拒。
- Python 导入器：函数级 AST -> AGL 源码，翻不了的（class/lambda/推导式/f-string 带格式说明符/无注解参数/
  全局变量/具体 except/列表.append 表达式形式等）**逐条直说原因**，绝不瞎猜。
  **v0.11 起已支持**：f-string（无格式说明符）、链式比较、字符串 `in`/`not in`、下标赋值、
  映射表键赋值、下标增广赋值、纯字面量多目标赋值、三元表达式（赋值右边）、默认参数（带 warning）、
  字符串方法调用（upper/lower/strip/replace/split）、append 语句形式。

---

## 8. 可以提优化建议的方向（已知薄弱点，欢迎具体 PR/建议）

> 提建议时请给出：**现象 → 最小复现 → 期望行为 → 风险提示**。不要只说"应该更快/更好"。

1. **类型推断偏弱**：下标/成员目前不读元素类型（已补一点：已知 `列表<文本>` 时 `x[0]` 为文本）；
   函数间效应传播、未标注函数默认"无效应"都还有盲区。
2. **导入器覆盖面仍有盲区**：函数级基本写法已覆盖（v0.10 新增 f-string / 链式比较 / in / 下标赋值 / 增广下标 / 多目标赋值）；但 class、嵌套函数、f-string 格式说明符、推导式、lambda、全局变量、具体 `except`、列表原地方法、三元表达式、默认参数、`with`、`assert`、`raise` 仍拒。这是阶段 3 的主战场，优先级高。
3. **字符串插值**：AGL 无原生 f-string 等价，导入器直接拒 `f"..."`。可设计 `拼接("值=", x)` 之外的语法。
4. **后端单一**：目前只有 Python 后端；IR→JS、IR→C 是 roadmap（阶段 2 规划）。
5. **优化 Pass 仍保守**：可加跨函数常量传播、循环不变量外提等，但**必须过对拍**才准合入。
6. **控制流**：无 `match`/模式匹配（规划里标"阶段 2 之后"）；`中断`/`继续` 在 for 的限制可再核。
7. **行号对齐的 1 行边界**（见 §4 铁律 1）是否值得用更复杂的后端技巧消除，可论证。

---

## 9. 给协作 AI 的工作约定

- **先备份→验证备份成功→再删/改** 是个人文件铁律；代码改动走版本化测试，不碰未授权文件。
- **不手改生成物**（语法文件、golden 产物）；改动后跑刷新命令并复检。
- **每条修复走"回退复现 → 修 → 复测"**，并在 `docs/标准与契约.md` 的变更记录加一条 vX.Y 条目。
- 改动 IR 字段必须同步 `spec/ir-schema.json`（契约锁死）。
- 给用户的回复用**简体中文、直白简短**；这个人是非技术背景，抵触术语堆砌。
- 训练域（AI 助手训练代码/配置）是**禁区**，未经明确开口不要动。

---

### 附：本仓库记忆文件（跨会话上下文）

- `.workbuddy/memory/YYYY-MM-DD.md` 每日工作流水（append-only）。
- `.workbuddy/memory/MEMORY.md` 跨会话铁律与目录要点。
- `docs/标准与契约.md` 是最权威的规格与 30 条硬规则清单。
