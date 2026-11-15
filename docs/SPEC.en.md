# AGL Language Specification (English)

> Version: **0.53.0**　Release date: 2026-11-15　Status: current release specification
>
> This document describes the language surface that is implemented and verified by `aglc doctor` in
> `v0.53.0`. The machine-readable source of truth is [`spec/keywords.toml`](../spec/keywords.toml).
> If this document and the keyword table disagree, the table and compiler behavior win.

## 1. Scope

AGL (Agent Grounded Language) is a declarative language for AI-generated code that humans must be
able to review. Fixed keywords, explicit types, explicit effects and source-line diagnostics turn
"what did the AI write, what will it touch, and where did it fail" into compiler-checked facts.

This specification covers: lexing, types, declarations, statements, expressions, effects, check
blocks, modules, runtime artifacts, diagnostics and the MCP entry point. It does not cover compiler
implementation details, editor UI details, or tool-runtime features that are not implemented in this version.

## 2. Source files and lexing

### 2.1 Encoding and branch

- A source file is UTF-8 text with the `.agl` extension.
- The first line must declare the branch:
  - Chinese branch: `#语言=中文`
  - English branch: `#lang=English`
- A file may use keywords from one branch only. An English keyword in a Chinese file is
  `AGL-E-0102`, and the reverse is also rejected.
- Identifiers, strings and comments are author content. They are **never translated or rewritten**.

### 2.2 Comments and whitespace

```text
// line comment
/* block comment */
```

Whitespace and newlines separate statements. Blocks always use `{ }`. A condition sits before the
block; no parentheses are required:

```agl
#lang=English
fn main() {
  let count = 3
  if count > 0 {
    print("has data")
  }
}
```

### 2.3 Identifiers and keywords

- Identifiers may contain Unicode letters, digits and underscores, and may not start with a digit.
- Keywords are reserved and cannot be used as variable, function, parameter or field names.
- Built-in function names are not keywords. A user definition wins on a name collision; using a
  built-in name as a variable is legal but discouraged.
- The full keyword table is in this document's keyword table; the full built-in table is available with
  `aglc card --lang en` or `aglc list-builtins`.

### 2.4 Literals

| Literal | Syntax | Notes |
|---|---|---|
| Int | `42`, `-7` | Decimal; a leading minus is unary negation |
| Float | `3.14`, `1.2e3` | Decimal point or scientific notation |
| Bool | `true` / `false` | Chinese branch uses `真` / `假` |
| Null | `null` | Chinese branch uses `空` |
| String | `"hello"` | Double quotes; common escapes supported |
| List | `[1, 2, 3]` | Elements should share a type |
| Map | `{"key": value}` | Empty map is `{}` |
| Set | `{1, 2, 3}` | Duplicates removed; empty set is `make_set()` |
| Struct instance | `User(id: 1, name: "Aming")` | Named-field construction |

## 3. Types

### 3.1 Primitive and container types

| English | Chinese | Meaning |
|---|---|---|
| `Int` | `整数` | Integer |
| `Float` | `浮数` | Floating-point number |
| `Bool` | `布尔` | True / false |
| `String` | `文本` | UTF-8 text |
| `Null` | `空` | Null type |
| `List` | `列表` | List; `List<Int>` specifies element type |
| `Map` | `映射表` | Key-value map |
| `Set` | `集合` | Deduplicated set |
| `Result` | `结果` | Success or failure; construct with `Ok(value)` / `Err(reason)` |
| `Error` | `错误` | Captured error object |
| `Any` | `任意` | Fallback when no static type can be determined |

### 3.2 Structs

```agl
type User {
  id: Int
  name: String
}
```

- Declare a struct with `type`; write one `field: Type` per line.
- Construction must use named fields: `User(id: 1, name: "Aming")`.
- Field access uses a dot: `a.id`.
- Unknown fields, missing fields and field type mismatches are compile-time errors.

### 3.3 Nullable types

- `T?` is a nullable type, for example `String?`.
- An ordinary value may be assigned to `T?`; `null` may only be assigned to `T?`.
- A `T?` value cannot flow directly into `T`; doing so is `AGL-E-0301`.
- This version does not perform flow-sensitive narrowing across branches. After a null check, the
  non-null value must still be extracted explicitly.

### 3.4 Inference and annotations

- `let name = expression` infers a type from the right-hand side.
- Function parameters, return types and struct fields should be annotated; explicit types take part
  in checking.
- Generic containers are written `List<Int>`, `List<User>`; `Result<String>` may be used in
  interface signatures.

## 4. Declarations

### 4.1 Functions

```agl
#lang=English
fn add(a: Int, b: Int) -> Int {
  return a + b
}

fn main() {
  print(add(2, 3))
}
```

- The entry function must be called `main` (Chinese: `主入口`); execution starts there.
- A function with a return type must return that type on every path.
- A function without a return type is not required to return a value.
- Parameters may have defaults; calls may use named arguments (`name: value`).

### 4.2 Variables and constants

```agl
#lang=English
fn main() {
  let count = 0
  const service_name = "agl-api"
  print(service_name, count)
}
```

- `let` bindings may be reassigned; `const` bindings may not.
- Augmented assignment is available: `+=`, `-=`, `*=`, `/=`, `%=`.
- Destructuring assignment is available: `let a, b = [1, 2]`.

### 4.3 Effects and check blocks

An effect declaration sits between the function header and the body; a check block may come before
or after it:

```agl
#lang=English
fn read_config(path: String) -> String
check {
  path != ""
}
effect IO
{
  return unwrap(read_file(path))
}

fn main() effect IO {
  let content = read_config("examples/en/sample.log")
  print(content.length)
}
```

- Effect labels: `IO` / `Net` / `model` / `Time` / `External` (Chinese: `读写` / `网络` / `模型` /
  `计时` / `外部调用`).
- A function that calls an effectful function must declare the same effect; otherwise the compiler
  reports `AGL-E-0401`.
- Conditions in `check` must be statically decidable comparisons or logical combinations. At
  runtime a false condition aborts immediately with `check failed: <source text>`.
- Check blocks are enabled by default and cannot be disabled.

### 4.4 Async and FFI

- `async fn` declares an asynchronous function; call it with `await`.
- `task(...)` starts an async task and may be combined with `on_progress` / `on_finish` / `on_fail`
  callbacks.
- `import ffi lib [effect ...]` declares an external library. FFI calls count as the worst-case
  effect unless a smaller effect is declared explicitly.
- FFI is black-box only; external library source is not imported.

### 4.5 Modules, imports and exports

```agl
#lang=English
module BookLibrary

export fn discounted(price: Float, discount: Int) -> Float {
  return price * (1.0 - discount / 100.0)
}
```

```agl
#lang=English
import BookLibrary

fn main() {
  print(BookLibrary.discounted(100.0, 20))
}
```

- `module Name` declares a module. The module name is the file name and is not translated.
- `export` controls what other modules may use. Unexported functions and types are private.
- Import with `import Name` and call with `Name.function(...)`.
- A module that calls the standard library must import it itself; a main-program import does not
  propagate into the module.

### 4.6 Interfaces and dependencies

```agl
#lang=English
interface Payment {
  fn pay(amount: Int) -> Result<String>
}

fn pay(amount: Int) -> Result<String> {
  return Ok("paid")
}

fn main() {
  print(pay(100))
}
```

- An interface contains signatures only. The same file must contain implementations whose
  parameter count, parameter names, parameter types and return type match exactly.
- A dependency block is written `deps { toolkit = "1.2.0" }`. Versions must be
  `major.minor.patch` strings; source dependencies are cross-checked against
  `agl.toml [dependencies]`.

## 5. Statements

| Statement | English syntax | Chinese syntax |
|---|---|---|
| Conditional | `if cond { } else if cond { } else { }` | `如果 条件 { } 否则 如果 条件 { } 否则 { }` |
| While | `while cond { }` | `当 条件 { }` |
| For-each | `for item in list { }` | `循环 每个 在 列表 { }` |
| Return | `return value` | `返回 值` |
| Break / continue | `break` / `continue` | `中断` / `继续` |
| Match | `match (expr) { literal { } _ { } }` | `匹配 (表达式) { 字面量 { } _ { } }` |
| Try | `try { } catch (e) { } finally { }` | `尝试 { } 捕获 (错) { } 最后 { }` |
| Typed catch | `catch (e: IndexError)` | `捕获 (错: 下标越界)` |
| With | `with expr as name { }` | `使用 表达式 作为 名 { }` |
| Await | `await async_call()` | `等待 异步调用()` |

`match` patterns currently support only literals and the `_` wildcard. A match with no matching arm
and no fallback executes nothing.

## 6. Expressions and operators

### 6.1 Comparison and logic

| English | Chinese | Meaning |
|---|---|---|
| `==` | `等于` | Equal |
| `!=` | `不等于` | Not equal |
| `>` | `大于` | Greater than |
| `<` | `小于` | Less than |
| `>=` | `大于等于` | Greater than or equal |
| `<=` | `小于等于` | Less than or equal |
| `&&` | `并且` | Logical AND, short-circuiting |
| `||` | `或` | Logical OR, short-circuiting |
| `!` | `非` | Logical NOT |

Arithmetic uses `+` `-` `*` `/` `%`. Prefer the word forms above for comparisons and logic in the
Chinese branch.

### 6.2 Members, indexing and calls

- Length is the read-only `.length` property (Chinese `.长度`) on strings, lists, maps and sets.
- Lists and strings use `[i]`, zero-based; maps use `[key]`.
- Function calls use `name(args...)`; module calls use `module.function(...)`; fields use
  `value.field`.
- `call_fn(fn_ref, args...)` dynamically invokes a function reference in the form
  `[function_name, captures...]`.

## 7. Keyword reference

The table below comes from `spec/keywords.toml`. A Chinese file may use only the Chinese column; an
English file may use only the English column.

| Category | English | Chinese |
|---|---|---|
| Declarations | `fn` `let` `const` `type` `effect` `import` `export` `module` `check` `interface` `deps` | `函数` `变量` `常量` `类型` `效应` `导入` `导出` `模块` `校验` `接口` `依赖` |
| Modifiers | `async` `ffi` | `异步` `外部` |
| Statements | `if` `else` `for` `in` `while` `return` `match` `with` `as` `try` `catch` `finally` `break` `continue` `await` `on_progress` `on_finish` `on_fail` | `如果` `否则` `循环` `在` `当` `返回` `匹配` `使用` `作为` `尝试` `捕获` `最后` `中断` `继续` `等待` `当收到进度` `当任务完成` `当任务失败` |
| Types | `Int` `Float` `Bool` `String` `Null` `List` `Map` `Set` `Result` `Error` `Any` | `整数` `浮数` `布尔` `文本` `空` `列表` `映射表` `集合` `结果` `错误` `任意` |
| Effects | `IO` `Net` `model` `Time` `External` | `读写` `网络` `模型` `计时` `外部调用` |
| Booleans | `true` `false` | `真` `假` |
| Entry | `main` `Ok` `Err` `print` | `主入口` `成功` `失败` `打印` |

`tool` / `工具`, `input` / `入参`, `config` / `配置`, `agent` / `智能体` and `rule` / `规则` are
extension keywords. This version treats them as ordinary names; they do not create a callable tool runtime.

## 8. Built-in functions

Built-ins are defined by the `[[builtin]]` table in `spec/keywords.toml`; there are currently
**187**. They require no import. Common groups:

| Group | Representative built-ins |
|---|---|
| Output and text | `print` `to_text` `fill` `split` `join` `replace` `strip` `upper` `contains` `find` `startswith` `endswith` |
| Containers | `append` `insert` `remove` `sorted` `reverse` `slice` `keys` `values` `items` `make_map` `make_set` |
| Numbers | `abs` `min` `max` `sum` `round` `pow` `format_number` `sqrt` |
| Results | `Ok` `Err` `unwrap` `fail_reason` |
| Files and system | `read_file` `write_file` `append_file` `exists` `list_dir` `path_join` `args` `cwd` |
| Data | `parse_json` `dump_json` `read_csv` `write_csv` `sqlite_open` `sqlite_query` `sqlite_exec` |
| Network | `fetch` (requires the `Net` effect) |
| Async | `task` `sleep` `report_progress` `cancel` |

The full list, including parameters, effects and examples, is available from `aglc card` /
`aglc list-builtins`. Built-in effects obey the effects rules in this document.

## 9. Modules and projects

### 9.1 `agl.toml`

```toml
[project]
name = "my-agent"
version = "0.1.0"
entry = "main.agl"
language = "en"

[dependencies]
"math" = "0.1.0"
```

- `entry` points to the entry file; `language` is `zh` or `en`.
- `[dependencies]` records project dependencies; a source `deps` block must agree with it.
- Modules inside a project resolve by file name; imports omit the extension.

### 9.2 Standard library

Current standard-library modules are `math`, `list_util`, `text_util`, `date_util`, `stats_util`,
`crypto_util` and `unit_convert` (Chinese names: `数学`、`列表工具`、`文本工具`、`日期工具`、`统计工具`、
`加密工具`、`单位转换`). They are written in AGL, are pure and have no effects. Declare the dependency
in `agl.toml` and write an `import` in the source.

## 10. Runtime model and artifacts

- `aglc check <file>`: lexical, syntax, semantic, effect and module checks; produces no files.
- `aglc build <file>`: uses the self-hosted compiler by default; produces `<name>.agb` (AGB bytecode)
  and `<name>.agl.ir`.
- `aglc run <file>`: compiles and executes on the default VM; runtime errors map back to the AGL
  file, line and function.
- `.agb` files can be run directly: `aglc run <file.agb>`.
- `--compiler seed` falls back to the Python seed compiler; `--backend py` is an explicit Python
  backend fallback, not the default path.
- The VM supports an effect allow-list: `--allow-effects IO,Time,Net,External`; an effect outside the
  list reports `AGL-RT-0092`.
- The default VM has a step limit and a wall-clock timeout; exceeding them reports
  `AGL-RT-0091` / `AGL-RT-0095`.

## 11. Diagnostics

Diagnostic ranges:

| Range | Category |
|---|---|
| `AGL-E-01xx` | Lexing |
| `AGL-E-02xx` | Syntax |
| `AGL-E-03xx` | Semantics / types |
| `AGL-E-04xx` | Effects / checks |
| `AGL-E-05xx` | IR / lowering |
| `AGL-E-06xx` | Modules / configuration |
| `AGL-RT-00xx` | Runtime |

A structured diagnostic contains at least: code, severity, Chinese message, AGL file/line/column,
call stack, effects and fix hints. Host-language terms do not appear in `message_zh`; the host cause
is kept in the `cause` field.

## 12. MCP entry point

- Server name: `agl-mcp`
- Server version: **0.53.0** (follows `meta.version` in `spec/keywords.toml`; not hard-coded)
- Protocol version: **2024-11-05**
- Tool count: **10**

Tools: `agl_card`, `agl_check`, `agl_check_source`, `agl_build`, `agl_run`, `agl_run_source`,
`agl_convert`, `agl_summary`, `agl_version`, `agl_doctor`.

Configuration:

```jsonc
{
  "command": "python",
  "args": ["-m", "compiler.cli", "mcp"]
}
```

Verified version command: `python -m compiler.mcp --version` → `agl-mcp 0.53.0 (protocol 2024-11-05)`.
`agl_version` returns `server` / `version` / `languageVersion` / `protocolVersion`; when
`GITHUB_RELEASES_URL` is empty, `checkUpdate=true` returns `not_configured` without network access.

## 13. Versioning and compatibility

- Language versions follow semantic `major.minor.patch`; the authoritative value is `meta.version`
  in `spec/keywords.toml`.
- `pyproject.toml`, `agl-mcp` and MCP `languageVersion` must match that value.
- New keywords, types, effects or semantic changes are minor/major changes; bug fixes and
  documentation are patch changes.
- Golden artifacts from older versions are not a long-term compatibility promise; run `doctor` and
  refresh them before a release.

## 14. Known limits

The following are **not** implemented in this version and must not be implied by examples or
documentation:

- tuples, classes, inheritance, generic functions, closures, lambdas, first-class function values;
- flow-sensitive null narrowing across branches;
- a real MCP / local-skill / ACP tool-call runtime (`tool` blocks are still extension keywords);
- an OS-level runtime sandbox; current safety is **compile-time effect declaration checking**, not
  system permission isolation;
- repeat-call circuit breaking (design only, no runtime code);
- standard-library version locking and a package manager.

## 15. Verification

From the root of the extracted AGL directory, run:

```powershell
.\aglc.cmd doctor
python examples/run_examples.py
```

The runtime package passes **5/5** (`doctor`); the source tree passes **27/27**. The example verifier checks and runs all six examples and compares their expected output. Actual output is recorded in [`examples/README.md`](../examples/README.md).

The minimal runnable block above is verified as `examples/zh/00_最小程序.agl`.
