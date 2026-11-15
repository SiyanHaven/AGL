# AGL（Agent Grounded Language）v0.53.0

AGL 是一门面向 AI 生成、同时要求人类可审查的声明式语言。固定关键字、显式类型、显式效应，编译期和运行期错误都指回 `.agl` 源码行号。

## 快速开始

在本目录执行：

```powershell
.\aglc.cmd doctor
```

看到 **5 项全部通过（运行时包）** 即安装完成。

```powershell
.\aglc.cmd init 我的Agent
.\aglc.cmd run 我的Agent\main.agl
```

也可以先安装成全局命令：

```powershell
python -m pip install "https://github.com/SiyanHaven/AGL/releases/download/v0.53.0/agl_lang-0.53.0-py3-none-any.whl"
aglc doctor
```

## 文档

- [安装与更新](docs/安装与更新.md)：在线安装、离线包、常用命令、MCP
- [入门教程](docs/入门教程.md)：第一个程序、基本语法、内建函数
- [语言规范（中文）](docs/SPEC.zh.md) / [Language Specification (English)](docs/SPEC.en.md)
- [设计概览](docs/设计概览.md)：目标、编译流水线、目录、能力边界
- [路线图](docs/路线图.md)：后续版本要做什么
- [发布说明](docs/发布说明.md)：这一版提供什么、验证结果、已知边界
- 示例：发布包 `examples/` 内含六个完整的中英示例，另有最小程序

## 包内文件

```text
README.md  LICENSE  CHANGELOG.md
CONTRIBUTING.md  CODE_OF_CONDUCT.md  SECURITY.md  SUPPORT.md  CITATION.cff
aglc.cmd  pyproject.toml
compiler/  aglc/  spec/  stdlib/  examples/
docs/      规范、教程、安装、设计概览、路线图、发布说明
```

`compiler/_runtime/自举编译器.agb` 是预编译的编译引擎。普通用户只写 `.agl`，不需要看或改自举源码。

发布包只保留运行时、公开文档和示例；测试、编辑器扩展和开发用脚本不随发布包提供。

## MCP

```json
{
  "command": "<本目录绝对路径>\\aglc.cmd",
  "args": ["mcp"]
}
```

## 项目主页

https://github.com/SiyanHaven/AGL
