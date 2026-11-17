# AGL（Agent Grounded Language）

**一门让不懂编程的人也能审查 AI 在干什么的声明式语言。**

AGL 把 AI 生成的代码变成可检查的结构：固定关键字、显式类型、显式效应（会读文件、会联网、会调用外部程序），错误永远指回 AGL 源码的行号。源码先变成自研 AGB 字节码，再由默认 VM 执行；Python 只负责启动，不生成 `.py` 产物。

当前版本：**v0.53.0**。仓库：https://github.com/SiyanHaven/AGL

普通用户装的是**运行时包**：不需要看、也不需要改 AGL 的编译器源码。`aglc doctor` 报运行时自检 **5/5**，八个发布示例中六个短进程批跑 **6/6** 通过，双语规范代码块 **12/12** 通过。完整的 29 项验收只在源码树/开发环境跑。

## 普通用户：在线安装

先让 agent 用仓库地址找到 Release 里的 wheel 资产，再执行；`:v0.53.0` 是发布标签，前提是该 Release 已上传 `agl_lang-0.53.0-py3-none-any.whl`：

```powershell
python -m pip install "https://github.com/SiyanHaven/AGL/releases/download/v0.53.0/agl_lang-0.53.0-py3-none-any.whl"
aglc doctor
```

装的是 433 KB 的 wheel，只下载这个运行时包，不 clone 源码。不要改用 `pip install git+https://...`，那种方式会把整个仓库（含 `自举/`、`tests/`、`editor/`）下载下来再构建。

**安装前先核对 SHA256**：Release 说明里会给出 wheel 的哈希；下载后先用 `Get-FileHash ... -Algorithm SHA256`（Linux/macOS 用 `sha256sum`）比对，不一致就不要安装。项目暂时只提供哈希，没有 GPG 签名；没有公布哈希的 Release 视为未校验下载，不要直接装进有敏感权限的环境。详细步骤见 `docs/安装与更新.md`。

看到 **5 项全部通过（运行时包）** 即安装完成。装完后直接用：

```powershell
aglc init 我的Agent
aglc run 我的Agent/main.agl
```

用户包只含运行时需要的部分：`compiler/`、`aglc/`、`spec/`、`stdlib/`、`examples/`，外加一个预编译的 `compiler/_runtime/自举编译器.agb`。它**不含** `自举/*.agl` 源码、`tests/`、`editor/`；`pip install` 不会下载这些目录，首次 `aglc run`/`aglc build` 也不会跑 40 秒自举链，只是把内置 `.agb` 复制到用户目录（默认 `~/.agl`，可用 `AGL_HOME` 覆盖）。

那个 `.agb` 是**编译引擎文件**，相当于编译器的可执行版本，不是给用户读或改的源码。普通用户只写 `.agl`，`aglc` 用它把 `.agl` 编成 `.agb` 再执行。自举源码仍然放在 GitHub 仓库的 `自举/` 里，只给开发者。

安装后 `aglc` 由当前 Python 的 `Scripts` 提供；MCP 直接启动，不需要源码目录：

```json
{
  "command": "python",
  "args": ["-m", "compiler.cli", "mcp"]
}
```

如果 agent 的 Python 不在 PATH，用该 Python 的绝对路径执行 `pip` 和 `aglc`。

MCP 的文件路径默认限制在启动时的当前目录内；需要访问更大的项目根时，在启动配置里设置
`AGL_MCP_WORKSPACE`，不要直接把任意绝对路径交给工具。

## 开发者：源码树/完整自举

```powershell
git clone https://github.com/SiyanHaven/AGL.git
cd AGL
python -m compiler.cli doctor
```

源码树保留 `自举/`、`tests/`、`editor/`，`doctor` 跑完整 **29 项**。改编译器、跑黄金用例、看自举源码，都在这个形态里做；这与普通用户的安装包是两套形态，互不下载。

## 压缩包（离线/普通用户）

下载 Release 里的 `AGL-v0.53.0.zip`，解压到任意目录，在该目录中执行：

```powershell
.\aglc.cmd doctor
.\aglc.cmd run examples\zh\00_最小程序.agl
```

这是干净的运行时包，`doctor` 跑 **5 项**。包内只有 `compiler/`、`aglc/`、`spec/`、`stdlib/`、`examples/`、`aglc.cmd`，不含 `tests/`、`自举/`、`editor/`、设计规划或开发文档。

## 三十秒上手（源码树）

源码树零第三方依赖，不捆绑 Python：默认直接使用当前 `python`（Python 3.10+）。在仓库根目录执行：

```powershell
# 1. 体检：源码树跑 29 项，必须全绿
python -m compiler.cli doctor

# 2. 跑一个最小示例
python -m compiler.cli run examples/zh/00_最小程序.agl
#   → 有数据

# 3. 跑一个真实应用示例
python -m compiler.cli run examples/zh/01_销售日报.agl

# 4. 编译成 AGB 字节码，再由默认 VM 执行
python -m compiler.cli build examples/zh/01_销售日报.agl --out build/examples
python -m compiler.cli run build/examples/01_销售日报.agb

# 5. 一次验证全部发布示例
python examples/run_examples.py
#   → EXAMPLES OK: 6/6 (check + build .agb + VM run)
```

仓库根目录也提供 `aglc.cmd`，可直接用 `aglc` 代替 `python -m compiler.cli`。需要全局命令时可执行 `python -m pip install -e .`。



***

## 发布示例

六个示例都是可编译、可运行、可逐字核对输出的应用，不是一次性片段：



| 分支 | 示例                                                          | 演示内容                 |
| -- | ----------------------------------------------------------- | -------------------- |
| 中文 | [zh/00\_最小程序.agl](examples/zh/00_最小程序.agl)                  | 最小入口、条件              |
| 中文 | [zh/01\_销售日报.agl](examples/zh/01_销售日报.agl)                  | 结构体、列表、循环、金额与完成率     |
| 中文 | [zh/02\_待办清单.agl](examples/zh/02_待办清单.agl)                  | 结构体列表、函数式更新、完成计数     |
| 中文 | [zh/03\_日志分析.agl](examples/zh/03_日志分析.agl)                  | UTF-8 文件读取、`结果`、逐行统计 |
| 英文 | [en/01\_sales\_report.agl](examples/en/01_sales_report.agl) | 与中文销售日报同逻辑           |
| 英文 | [en/02\_todo\_list.agl](examples/en/02_todo_list.agl)       | 与中文待办清单同逻辑           |
| 英文 | [en/03\_log\_analyzer.agl](examples/en/03_log_analyzer.agl) | 与中文日志分析同逻辑           |

完整预期输出、构建方式和设计约束见 [examples/README.md](examples/README.md)。

### 中文分支



```
#语言=中文
函数 主入口() {
  变量 数量 = 3
  如果 数量 大于 0 {
    打印("有数据")
  }
}
```

### 英文分支



```
#lang=English
fn main() {
  let count = 3
  if count > 0 {
    print("has data")
  }
}
```

两个分支的关键字不同；**标识符、字符串、注释永远不翻译**，它们属于作者，不属于编译器。



***

## 为什么要有 AGL

AI 写的 Python，普通人读不懂，只能选择 “相信” AI，没法 “审查” AI。AGL 做两件事：



1. **写的时候**：把目标、约束、会碰什么（效应）写成结构化代码，编译器强制检查；

2. **错的时候**：错误指向 AGL 源码的行号与函数，而不是 `AttributeError: 'NoneType' object has no attribute ...`。

三条不能忘的规矩：



1. 改完必须跑 `python -m compiler.cli doctor`，29 项全绿才算没弄坏；

2. 期望产物变了，先逐条解释差异，再更新黄金产物；解释不了就是有 bug；

3. 标识符、字符串、注释永远不翻译。



***

## 文档入口



| 顺序 | 文件                                                                      | 读它是为了                          |
| -- | ----------------------------------------------------------------------- | ------------------------------ |
| 1  | [docs/SPEC.zh.md](docs/SPEC.zh.md) / [docs/SPEC.en.md](docs/SPEC.en.md) | 中文 / 英文语言规范：词法、类型、语句、效应、模块、MCP |
| 2  | [examples/README.md](examples/README.md)                                | 八个发布示例、预期输出、构建方式               |
| 3  | [docs/入门教程.md](docs/入门教程.md)                                            | 从安装到第一个程序                      |
| 4  | [docs/标准与契约.md](docs/标准与契约.md)                                          | 唯一内部权威标准：契约、验收项、改代码 SOP、能力边界   |
| 5  | [docs/自举路线.md](docs/自举路线.md)                                            | 用 AGL 写 AGL 编译器已做到哪一步          |
| 6  | [docs/阶段2拆解.md](docs/阶段2拆解.md)                                          | 下一阶段要做哪些包、先做哪个                 |
| 7  | [docs/网页应用.md](docs/网页应用.md)                                            | 可编程 HTTP 服务、请求/响应映射表、完整留言板案例 |

规范以 [spec/keywords.toml](spec/keywords.toml) 为机器可读权威源；规范文档与关键字表不一致时，以表和编译器实际行为为准。



***

## MCP 接入

`agl-mcp` 是给 AI 客户端调用的 stdio 服务器。当前事实：



* 服务器版本：**agl-mcp 0.53.0**（跟随 `spec/keywords.toml` 的 `meta.version`，不硬编码）

* 协议版本：**2024-11-05**

* 工具数量：**10**

工具：`agl_card`、`agl_check`、`agl_check_source`、`agl_build`、`agl_run`、`agl_run_source`、`agl_convert`、`agl_summary`、`agl_version`、`agl_doctor`。

`agl_run` / `agl_run_source` 默认全禁效应和外部库；需要时显式传 `allow_effects` / `allow_tools`（逗号分隔，`all` 才全量放行）。

### 工具声明调用（MCP）

`工具` 声明现在会变成真实可调用的 MCP 工具，不再只是设计词。声明里写清楚协议、地址和入参，函数体内直接按工具名调用：

```agl
工具 天气 {
  配置 {
    协议 = "mcp"
    地址 = "http://127.0.0.1:18999/mcp"
    超时秒数 = 5
  }
  入参(城市: 文本)
  返回 文本
}

函数 主入口() 效应 外部调用 {
  变量 答复 = 天气("杭州")
  打印(答复)
}
```

`aglc run` 默认拒绝外部调用，必须显式放行：

```powershell
aglc run 天气.agl --allow-effects External
```

当前工具运行时只实现 **MCP Streamable HTTP**（`协议 = "mcp"`）；`local-skill` / `acp` 还没接。直接按工具名调用会在编译期降成内建 `调用工具`，参数是工具名、实参映射表和配置映射表；也可以直接写 `调用工具("天气", {...}, {...})`。



在线 pip 安装后，MCP 不需要源码目录：

```jsonc
{
  "command": "aglc",
  "args": ["mcp"]
}
```

压缩包形态则指向解压目录：

```jsonc
{
  "command": "python",
  "args": ["-m", "compiler.cli", "mcp"],
  "cwd": "<解压后的 AGL 目录>"
}
```

验证命令：



```
python -m compiler.mcp --version
#   → agl-mcp 0.53.0（协议 2024-11-05）
python -m compiler.mcp --list
#   → 10 个工具清单
```

`agl_version` 返回 `server` / `version` / `languageVersion` / `protocolVersion`；`GITHUB_RELEASES_URL` 当前为空，`checkUpdate=true` 时返回 `not_configured`，不会静默联网。



***

## 仓库结构



```
spec/          五份共享契约 + keywords.toml（改它 = 改语言）
compiler/      编译器：lexer → parser → checker → ir → backend_py → vm → cli
自举/          自举编译器源码：词法器、语法分析器、编译器、中间表示、字节码
stdlib/        用 AGL 自己写的最小标准库（纯函数、零效应）
examples/      中英双语发布示例 + 一键验证脚本
docs/          双语规范、入门教程、标准与契约、发布文档、规范片段
scripts/       发布验证与快照脚本
tests/golden/  黄金用例（中英成对 + 期望 IR + 期望产物）
tests/cases/   错误用例 + expect.toml + 标准库 / 配置 / 优化用例
tests/suite/   一致性测试套件（29 项；doctor 只是它的壳）
editor/vscode/ VSCode 扩展（零 npm 依赖）
studio/        .NET WinForms 桌面宿主
```

更细的分层说明见 [docs/标准与契约.md](docs/标准与契约.md)。

`tests/`、`自举/`、`editor/` 只在源码树/完整包里；普通用户的 wheel 不含这些目录。



***

## 命令一览



| 命令                              | 作用                                                         |
| ------------------------------- | ---------------------------------------------------------- |
| `check <file>`                  | 只做词法、语法、语义、效应、模块检查                                         |
| `build <file>`                  | 产出 AGB 字节码 `.agb` 和中间表示 `.agl.ir`；默认自举编译器                  |
| `run <file>`                    | 编译并执行，错误回译到 AGL 行号；默认自研 VM + 自举编译器；`.agb` 可直接执行            |
| `bootstrap`                     | 安装自举编译器：源码树跑不动点链，用户包直接复制内置 `.agb`             |
| `summary <file>`                | 中文摘要（给人看）                                                  |
| `convert <file> --to zh\|en`    | 中英转换（只翻关键字）                                                |
| `migrate <file\|dir> [--write]` | 迁移旧关键字；默认只报告                                               |
| `compare <a> <b>`               | 两份代码结构是否等价（忽略标识符）                                          |
| `roundtrip <file>`              | 往返校验（必须逐字还原）                                               |
| `card [--lang zh\|en]`          | 关键字速查卡（写码前读）                                               |
| `init [dir]`                    | 生成能直接跑的工程骨架                                                |
| `examples`                      | 列出 `examples/` 下的发布示例                                      |
| `doctor`                        | 自检：用户包 5 项；源码树/完整包 29 项                                  |
| `mcp`                           | 启动 MCP 工具服务器（stdio）                                        |
| `lsp`                           | 启动语言服务器（stdio）                                             |
| `grammar [--dir X]`             | 输出编辑器语法高亮                                                  |
| `tokens <file>` / `ir <file>`   | 调试用                                                        |
| `dis <file>`                    | 编译成 AGB 字节码并反汇编                                            |
| `vm <file>`                     | 用 AGB 解释器执行；`--max-steps`、`--timeout-ms`、`--allow-effects` |

所有命令都支持 `--json`，方便 AI 自循环修复。



***

## 编译与执行矩阵

源码树里 `python -m compiler.cli bootstrap` 会跑一次不动点链（约 40 秒）安装 AGL 自己写的编译器到 `build/自举编译器/`。普通用户不需要这一步：用户包装完首次 `run`/`build` 会自动复制内置 `.agb`。之后两个编译器并存：



| 命令                                    | 编译器          | 静态校验                  | 适用            |
| ------------------------------------- | ------------ | --------------------- | ------------- |
| `aglc build/run <文件>`                 | 自举（默认）       | 编译前自动过种子全链 `check` 闸门 | 日常首选          |
| `aglc build/run <文件> --compiler seed` | Python 种子    | 全量（类型 / 效应 / 模块）      | 回退 / 对照       |
| `aglc run <文件> --backend py`          | 种子 Python 后端 | 全量                    | AGB 子集之外的显式兜底 |

`.agb` 是默认产物；`--backend py` 是显式兜底，不是默认路径。



***

## 发布验证（源码树）



```
python -m compiler.cli doctor
python examples/run_examples.py
python scripts/verify_spec_snippets.py
```

期望：



```
doctor: 28/28
EXAMPLES OK: 6/6 (check + build .agb + VM run)
SPEC SNIPPETS OK: 12/12
```

Windows 启动链默认跟随用户 agent 工具调用的 `python`；也可用环境变量 `AGL_PYTHON` 覆盖，或在 `python-path.txt` 写解释器绝对路径。发布包不捆绑 Python，桌面版找不到解释器时在输出区给出明确提示。



***

## 阶段 1 的安全边界（必须如实对外表述）

**阶段 1 的安全 = 编译期效应声明校验**：函数调用了文件、网络、模型、计时或外部调用，就必须声明对应效应，否则编不过。

默认 VM 另外支持可选的效应白名单（`--allow-effects IO,Time,Net,External`），但这只是运行参数，**不是 OS 级沙箱**。

沙箱、权限、配额、审批属于阶段 2；当前版本没有操作系统级隔离。

详见 [docs/标准与契约.md](docs/标准与契约.md) §8.3 和规范第 13 节 “已知边界”。



***

## 已知边界（不夸大）

当前版本**未实现**：元组、类、继承、泛型函数、闭包、lambda、一等函数值；跨分支的空值流敏感收窄；`local-skill` / `acp` 协议的工具运行时（MCP Streamable HTTP 已可用）；OS 级运行时沙箱；重复调用熔断 T19；标准库版本锁定与包管理器。

这些边界在 [docs/SPEC.zh.md](docs/SPEC.zh.md) / [docs/SPEC.en.md](docs/SPEC.en.md) 和 [docs/标准与契约.md](docs/标准与契约.md) 中有同样表述。



***

## 参与贡献

欢迎提交 issue、示例和文档修正。开始前请先读：



* [CONTRIBUTING.md](CONTRIBUTING.md)：开发环境、提交前检查、示例与规范同步要求

* [CODE\_OF\_CONDUCT.md](CODE_OF_CONDUCT.md)：社区行为准则

* [SECURITY.md](SECURITY.md)：安全问题报告方式

## 许可证

本项目采用 MIT License，详见 [LICENSE](LICENSE)。

## 更新记录

版本变化、破坏性变更和验证结果见 [CHANGELOG.md](CHANGELOG.md)。