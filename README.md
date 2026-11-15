<div align="center">

# AGL

**Agent Grounded Language**

面向 AI 生成、同时要求人类可审查的声明式语言。

固定关键字 · 显式类型 · 显式效应 · 错误回指源码行号

![Release](https://img.shields.io/badge/release-v0.53.0-2ea043?style=flat-square)
![License](https://img.shields.io/badge/license-MIT-blue?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-2024--11--05-6f42c1?style=flat-square)

[快速开始](#快速开始) · [MCP 在线安装](#mcp-在线安装) · [文档](#文档) · [源码结构](#源码结构)

</div>

---

## 为什么是 AGL

| 目标 | 实现 |
|---|---|
| 人类能读 | 中文、英文两套关键字，同一逻辑可双向转换 |
| AI 能写 | 固定关键字、显式类型、显式效应，生成约束明确 |
| 结果可查 | 编译期和运行期错误都回指 `.agl` 源码文件与行号 |
| 可演算 | 编译为 `.agl.ir` 与 `.agb`，由自研字节码 VM 执行 |

## 快速开始

### 在线安装（推荐）

```powershell
python -m pip install --upgrade "https://github.com/SiyanHaven/AGL/releases/download/v0.53.0/agl_lang-0.53.0-py3-none-any.whl"
aglc doctor
```

`aglc doctor` 显示 **5/5** 即安装完成。

### 离线安装

下载 [`AGL-v0.53.0.zip`](https://github.com/SiyanHaven/AGL/releases/download/v0.53.0/AGL-v0.53.0.zip)，解压后在目录中执行：

```powershell
.\aglc.cmd doctor
.\aglc.cmd init 我的Agent
.\aglc.cmd run 我的Agent\main.agl
```

### 第一个程序

```agl
#语言=中文

函数 主入口() {
  打印("你好，AGL！")
}
```

```powershell
aglc run 你好.agl
```

## MCP 在线安装

AGL 自带 `agl-mcp`，通过 stdio 暴露编译器能力，任何支持 MCP 的 AI 客户端都可以直接调用。当前版本为 **0.53.0**，协议为 **2024-11-05**，共 **10** 个工具。

### 1. 安装并自检

```powershell
python -m pip install --upgrade "https://github.com/SiyanHaven/AGL/releases/download/v0.53.0/agl_lang-0.53.0-py3-none-any.whl"
aglc mcp --version
```

期望输出：

```text
agl-mcp 0.53.0（协议 2024-11-05）
```

### 2. 配置客户端

把下面这一段放进客户端的 MCP 配置。不同客户端的配置文件位置不同，但 `command` / `args` 这一层结构一致：

```json
{
  "mcpServers": {
    "agl": {
      "command": "aglc",
      "args": ["mcp"]
    }
  }
}
```

如果客户端没有继承 `PATH`，把 `command` 换成：

```json
{
  "command": "python",
  "args": ["-m", "compiler.cli", "mcp"]
}
```

保存后重启客户端。MCP 是 stdio 长连接，重启或重连后才会加载新版本。

## 文档

| 文档 | 内容 |
|---|---|
| [安装与更新](docs/安装与更新.md) | 在线安装、离线包、常用命令、MCP |
| [入门教程](docs/入门教程.md) | 第一个程序、基本语法、内建函数 |
| [语言规范（中文）](docs/SPEC.zh.md) | 中文关键字、类型、效应、语法 |
| [Language Specification](docs/SPEC.en.md) | English keywords, types, effects, grammar |
| [设计概览](docs/设计概览.md) | 目标、编译流水线、目录、能力边界 |
| [发布说明](docs/发布说明.md) | 这一版提供什么、验证结果、已知边界 |
| [路线图](docs/路线图.md) | 后续版本方向 |

## 源码结构

| 目录 | 内容 |
|---|---|
| [`compiler/`](compiler/) | Python 编译器：词法、语法、类型、效应、IR、VM、MCP、LSP |
| [`aglc/`](aglc/) | 命令行入口与审计工具 |
| [`spec/`](spec/) | 关键字表与语言规范数据 |
| [`stdlib/`](stdlib/) | AGL 标准库 |
| [`examples/`](examples/) | 六个中英示例与最小程序 |
| [`自举/`](自举/) | 用 AGL 语言编写的自举编译器源码 |
| [`editor/`](editor/) | VS Code 语法高亮与语言服务扩展 |
| [`docs/`](docs/) | 公开文档 |

从源码运行：

```powershell
python -m compiler.cli doctor
python -m compiler.cli run examples/zh/00_最小程序.agl
```

首次运行会自动安装自举编译器。

## 版本

| 项目 | 值 |
|---|---|
| 版本 | 0.53.0 |
| MCP 协议 | 2024-11-05 |
| MCP 工具 | 10 |
| 许可证 | MIT |

## 参与贡献

缺陷报告、文档修正、示例和编译器改进都欢迎。较大的功能请先开 issue，说明语言表面和验收标准。提交前至少运行：

```powershell
python -m compiler.cli doctor
python examples/run_examples.py
```

## 许可证

[MIT](LICENSE)
