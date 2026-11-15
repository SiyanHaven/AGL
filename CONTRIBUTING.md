# Contributing to AGL

AGL 接受缺陷报告、文档修正、示例和编译器改进。较大的功能请先开 issue，确认语言表面和预期行为。

## 源码

源码、自举源码和编辑器扩展在：

https://github.com/SiyanHaven/AGL

```powershell
git clone https://github.com/SiyanHaven/AGL.git
cd AGL
python -m compiler.cli doctor
```

源码目录的 `doctor` 应通过 **5/5**。

## 提交前

至少运行：

```powershell
python -m compiler.cli doctor
python examples/run_examples.py
```

期望：

```text
doctor: 5/5（运行时自检）
EXAMPLES OK: 6/6
```

如果改了自举编译器，再运行：

```powershell
python -m compiler.cli bootstrap
```

## 语言变更

`spec/keywords.toml` 是机器可读的权威来源。语言变更必须在同一个 PR 中更新：

1. `spec/keywords.toml`；
2. 受影响的 lexer、parser、checker、IR、VM 或后端；
3. `docs/SPEC.zh.md` 与 `docs/SPEC.en.md` 的中英对应行为；
4. 对应的可运行示例或验证用例。

## 行为准则

参与项目即表示同意遵守 [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)。
