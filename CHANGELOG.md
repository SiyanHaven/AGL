# Changelog

All notable changes to AGL are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
semantic versioning, with the authoritative value in `spec/keywords.toml`
(`meta.version`).

## [Unreleased]

### Added

- `启动网页应用` / `web_serve`: a programmable HTTP server. Every request becomes a request map
  (`方法` / `路径` / `查询` / `Cookie` / `请求体` / `表单` / `头`), is dispatched to an AGL handler by name,
  and the handler returns a response map (`状态` / `类型` / `内容` / `文件` / `头`). JSON / form parsing,
  cookies, binary file responses with automatic MIME types, CORS, `HEAD` and `OPTIONS`, and unified
  `500` handling are built into the runtime.
- `examples/zh/04_经营分析.agl`: order validation, regional budget variance, customer ranking,
  risk notes and a Markdown report.
- `examples/zh/05_留言板网页.agl`: a complete SQLite-backed message board with HTML home page,
  JSON API, form POST and delete endpoint.
- `docs/网页应用.md`: request/response contract and the complete message-board example.
- `转义HTML` / `html_escape`, `编码URL` / `url_encode`, `解码URL` / `url_decode`: pure
  builtins for safely composing HTML text/attributes and URL components without a
  template engine.

### Fixed

- `打开数据库` now really returns `结果`; SQL execution builtins unpack either the result or the
  connection, so `取到(...)` and direct connection flows both work.
- The parser now recovers at top-level declaration boundaries and reports multiple independent
  syntax errors in one pass (`parse_many`); a broken entry file no longer hides every later error.
  A function body still stops at its first error.

### Security

- Web servers now default to loopback (`127.0.0.1`); `启动网页应用` accepts an optional third
  bind-address argument, and `"0.0.0.0"` must be written explicitly for LAN exposure.
- The built-in todo page escapes database text before inserting it into HTML (stored XSS fix).
- `获取网页` only accepts `http` / `https` URLs; `file://` and other schemes are rejected.
- Source reads accept UTF-8 BOM (`utf-8-sig`) across the CLI, module loader and MCP server.
- MCP file and build-output paths must stay inside the server workspace (default: current
  directory); set `AGL_MCP_WORKSPACE` to opt into a wider root.
- `aglc run` now defaults to **deny**: no effects and no external libraries unless
  `--allow-effects` / `--allow-tools` explicitly list them. Use `all` only when the caller
  deliberately trusts the program.
- The MCP `agl_run` / `agl_run_source` tools share that default: `allow_effects` and
  `allow_tools` are empty unless the caller passes them, and `all` is required for full access.
- `AGB` decoding validates module structure, opcodes, pool indices, jumps, call/task/try
  instruction shapes and entry points before execution—format/version alone is not trusted.
- `.agb` loading now validates opcode whitelist, pool indices, jump targets, function
  arrays, duplicate names and call/task/try structures in addition to format/version.
- `任务(异步函数())` emits `AGL-W-0401` when the started task declares effects that the
  current function has not declared; it no longer suppresses this case silently.
- `环境变量` refuses credential-looking names by default; the host can opt in with
  `AGL_ENV_ALLOW_SECRETS=NAME` or `all`.
- Parser recursion overflow now reports `AGL-E-0206` with file and line instead of a raw
  Python `RecursionError`; `cmd_info` no longer hard-codes a release number as fallback.
- `aglc audit` normalizes import aliases and `importlib.import_module` + `getattr` string
  concatenation, and states its static-analysis limits in the report.
- Self-hosted line tracking now carries through the full chain: lexer token line → AST
  statement `"行"` → IR statement `"行"` → `函数状态["当前行"]` → every `发射` call.
  The CLI-side Python line backfill was removed; selfhost AGB now has real line tables.

## [0.53.0] - 2026-11-15

Release candidate for the first public release.

### Added

- Bilingual language specifications: `docs/SPEC.zh.md` and `docs/SPEC.en.md`, covering lexing,
  types, declarations, statements, operators, effects, modules, diagnostics and the MCP entry point.
- Six runnable application examples under `examples/zh/` and `examples/en/`: minimal program,
  sales daily report, todo list and log analyzer. Each is checked, compiled to `.agb` and executed
  on the default VM.
- End-to-end example verifier `examples/run_examples.py` (`EXAMPLES OK: 6/6`).
- Spec snippet verification files under `docs/spec-snippets/` and
  `scripts/verify_spec_snippets.py` (`SPEC SNIPPETS OK: 12/12`).
- GitHub release files: README, LICENSE, CHANGELOG, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY,
  SUPPORT, CITATION, issue/PR templates and CI workflow.
- `aglc examples` now lists the actual files in `examples/` instead of embedding stale snippets.
- `doctor` installs the self-hosted compiler automatically when `build/自举编译器/` is missing, so a
  fresh release archive passes without a separate bootstrap step.
- `examples/run_examples.py` and `scripts/verify_spec_snippets.py` also bootstrap the compiler when
  needed, so both verifiers work from a freshly extracted archive.
- Release docs, editor instructions and packaging scripts no longer hardcode a developer-specific
  drive path or a personal interpreter path. `scripts/package_release.ps1` and `scripts/snapshot.ps1`
  derive the repository root from their own location, so they work from any directory and any drive
  letter.
- The release archive is scanned for project-local drive paths and `C:\Users\<user>` before
  publishing; the final package contains no such absolute paths and no `python-path.txt`.
- GitHub online installation now ships a **user runtime package**:
  `compiler/`, `aglc/`, `spec/`, `stdlib/`, `examples/`, plus the prebuilt
  `compiler/_runtime/自举编译器.agb`. The wheel excludes `自举/*.agl`, `tests/` and `editor/`;
  `pip install` does not download them and the first `aglc run`/`aglc build` copies the built-in
  `.agb` to `~/.agl` (or `AGL_HOME`) instead of running the 40-second bootstrap chain.
- `aglc doctor` has a runtime branch for the installed user package: 5 checks (spec/keywords,
  stdlib, builtin compiler, real compile+VM run, MCP metadata). The full 27 checks remain available
  only in the source tree / developer package.

### Changed

- MCP metadata is aligned with code: `agl-mcp 0.53.0`, protocol `2024-11-05`, **10 tools**.
  `agl_card`, `agl_check`, `agl_check_source`, `agl_build`, `agl_run`, `agl_run_source`,
  `agl_convert`, `agl_summary`, `agl_version`, `agl_doctor`.
- README rewritten for public release: current layout (`自举/`), quick start, examples, MCP facts,
  verification commands and honest capability boundaries.
- `docs/入门教程.md` points to `examples/zh/00_最小程序.agl`, not the removed `demo/`.
- `pyproject.toml` adds license, keywords and classifiers; removes the obsolete `demo*` package
  exclusion.
- Release launch chain uses the Python interpreter of the user's agent tool by default; the
  release archive does not bundle Python.

### Removed

- 30 stale `.agl` files at the repository root.
- The entire `demo/` directory (bookstore demo, v019/v020 snapshots, logs and generated Python).
- Two old generated Python files at the repository root.
- `scripts/show_pipeline.py`, which referenced the removed `demo/自举演示.agl`.
- `aglc/demo/agent_demo.py`, the last stale audit example; the `aglc/demo` directory is now empty.
- The removal list is recorded in `snapshots/removed-examples-20261115.txt`.

### Verification

- `python -m compiler.cli doctor` — 27/27.
- `python examples/run_examples.py` — 6/6 (check + build `.agb` + VM run, output compared).
- `python scripts/verify_spec_snippets.py` — 12/12.
- `python -m compiler.mcp --version` — `agl-mcp 0.53.0（协议 2024-11-05）`.
- `python -m compiler.mcp --list` — 10 tools.

### Known limitations

Not implemented in this version: tuples, classes, inheritance, generic functions, closures,
lambdas, first-class function values; flow-sensitive null narrowing; a real tool-call runtime;
OS-level runtime sandbox; repeat-call circuit breaker T19; standard-library version locking and a
package manager. See `docs/SPEC.zh.md` / `docs/SPEC.en.md` section 14 and
`docs/标准与契约.md` §8.3.
