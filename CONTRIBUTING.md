# Contributing to AGL

AGL accepts bug reports, documentation fixes, examples and compiler changes. For a large feature,
open an issue first so the language surface and acceptance criteria are agreed before code is
written.

By participating, you agree to follow [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).

## Development environment

- Python **3.10+** (3.13 is the current development interpreter).
- Zero third-party runtime dependencies. Do not add a runtime dependency without an issue and a
  maintainer decision.
- Windows is the primary desktop platform; the compiler and CLI should remain portable to Linux
  and macOS.
- Use the interpreter that your agent tool invokes. No bundled Python is shipped.

在解压/克隆后的 AGL 目录中执行：

```powershell
python -m compiler.cli doctor
```

The first `doctor` run is the baseline. A change is not ready if it makes `doctor` fail.

## Before opening a pull request

Run all three checks and paste their results into the PR:

```powershell
python -m compiler.cli doctor
python examples/run_examples.py
python scripts/verify_spec_snippets.py
```

Expected:

```text
doctor: 28/28
EXAMPLES OK: 6/6 (check + build .agb + VM run)
SPEC SNIPPETS OK: 12/12
```

If you changed the self-hosted compiler, also run:

```powershell
python -m compiler.cli bootstrap
python -m compiler.cli build examples/zh/01_销售日报.agl --out build/examples
python -m compiler.cli run build/examples/01_销售日报.agb
```

## Language changes

`spec/keywords.toml` is the machine-readable source of truth. A language change must update, in the
same pull request:

1. `spec/keywords.toml` (keyword, type, effect or built-in table);
2. lexer, parser, checker, IR and VM/backends as needed;
3. `docs/SPEC.zh.md` **and** `docs/SPEC.en.md` with the same behavior;
4. a runnable snippet under `docs/spec-snippets/zh/` and `docs/spec-snippets/en/`, registered in
   `scripts/verify_spec_snippets.py`;
5. golden cases under `tests/golden/` or error cases under `tests/cases/` when artifacts change;
6. `CHANGELOG.md` under “Unreleased”.

The Chinese and English branches must remain structurally equivalent. Keywords may differ;
identifiers, strings and comments are author content and must **never** be translated or rewritten.

## Examples

- Put published examples in `examples/zh/` or `examples/en/`.
- Every example must be deterministic, use no third-party package or network, and use a bundled
  fixture for file IO.
- Add the example and its expected output to `examples/run_examples.py`, then run it.
- Do not place compiler test fixtures in `examples/`; use `tests/golden/` and `tests/cases/`.

## Documentation

- `README.md` is user-facing; `docs/SPEC.*` is normative; `docs/标准与契约.md` is the internal
  contract.
- Commands in documentation must be copy-paste runnable. Do not add a command that is not
  implemented.
- When a version number appears in prose or JSON, it must match `spec/keywords.toml`. The MCP
  server version is derived from the keyword table; do not hard-code another value.
- State known limitations honestly. Do not imply a sandbox, tool runtime or standard-library
  version locking that does not exist.

## Code style

- Python: 4-space indentation, type hints where they clarify interfaces, no wildcard imports.
- AGL: follow `python -m compiler.cli card` for the branch you are writing.
- Diagnostics: messages are Chinese in `message_zh`, include the AGL file/line/column and a fix
  hint; do not leak Python traceback text into user-facing messages.
- Never commit generated `.agb`, `.agl.ir`, `build/`, `dist/` or editor cache files.

## Commits and pull requests

- One logical change per commit; write an imperative subject.
- Reference the issue number in the PR description.
- Describe the user-visible behavior, the test commands you ran and their results.
- Do not rewrite published history or force-push a shared branch.
- By contributing, you agree that your contribution is licensed under the MIT License.

## Reporting bugs

Use the bug report template and include:

- AGL version (`python -m compiler.cli --version`);
- Python version and OS;
- the smallest `.agl` file that reproduces the problem;
- the exact command, expected output and actual output;
- whether the default self-hosted compiler or `--compiler seed` was used.

For security issues, follow [`SECURITY.md`](SECURITY.md) instead of opening a public issue.
