# AGL 字节码（AGB）v0 草案

> 状态：**M0 格式/反汇编已完成**；**M1 栈式 VM 骨架已落地**（`aglc vm`，纯计算子集）；
> **M2 已进结构体、`尝试/捕获/否则/最后`、`当任务失败`、`匹配`、模块、`使用`（结果对象）、函数引用（键函数）、
> FFI（`导入 外部`）与异步任务（`任务` / `等待` / `当任务完成`）**；
> **M3 已有步数配额、IO / 计时 / 外部调用效应拦截、`--allow-effects` 宿主权限白名单**。
> **第一层已完成：`aglc build` 只产出 `.agb` + `.agl.ir`，不再产出 `.py`；`aglc run` 默认走 VM，
> 也能直接跑 `.agb`。超出 AGB 子集时明确报“该能力还不支持”，不会静默退回 Python；
> Python 只作 `--backend py` / `--backend auto` 的显式兜底。**
> 入口：`aglc dis <文件>` / `--json`、`aglc vm <文件>`、`aglc run <文件>`、`aglc run <文件>.agb`；
> `--backend auto` 在 AGB 可编译时自动走 VM，否则退回 Python。

## 1. 文件形态（已实现）

一份 AGB 对应一个模块，函数各自有独立的指令流：

```json
{
  "format": "AGB",
  "version": 0,
  "lang": "zh",
  "module": "main",
  "entry": "主入口",
  "consts": [null, "你好", 3],
  "names": ["print", "主入口", "计数", "加"],
  "types": [{ "name": "客户", "fields": ["名字", "年龄"] }],
  "init":  { "name": "<init>", "params": [], "code": [...], "lines": [...] },
  "functions": [
    { "name": "加", "params": ["a", "b"], "effects": [], "code": [...], "lines": [...] }
  ]
}
```

- `code` 与 `lines` **等长**：每条指令都带 AGL 源码行号，运行错误直接回译；
- `lang` 记录源语言分支（`zh` / `en`），`.agb` 不依赖源码文件也能按原分支执行；
- `consts` 只放不可变字面量（整数 / 浮数 / 文本 / 真假 / 空）；
- `names` 放变量名、函数名、内建名，**保留源码原文，不翻译**；
- `init` 执行模块顶层常量，然后才进入口函数；
- 模块单独编译成一份 AGB，导入时由加载器串起来。

## 2. 指令集（M0 已覆盖）

| 组 | 指令 | 语义 |
|---|---|---|
| 常量/变量 | `LOAD_CONST i` / `LOAD_NAME i` / `STORE_NAME i` | 压常量 / 读名字 / 写名字 |
| 算术 | `ADD SUB MUL DIV MOD NEG` | 与现有 AGL 语义一致（`/` 浮点除） |
| 比较 | `EQ NE LT LE GT GE` | 跨类型规则由 VM 统一执行，不交给宿主 |
| 逻辑 | `NOT`；`&&` / `||` 编译成 `JUMP_IF_FALSE` + `JUMP` + `LOAD_CONST` | 短路求值；右侧在左侧已决定结果时一条指令都不执行。`AND` / `OR` 仅保留给旧字节码，VM 仍可执行 |
| 跳转 | `JUMP n` / `JUMP_IF_FALSE n` | 绝对指令下标 |
| 调用 | `CALL_USER {name,argc}` / `CALL_BUILTIN {name,argc}` / `CALL_MODULE {module,name,argc}` / `CALL_FFI {lib,name,argc}` / `RETURN` | 本模块函数 / 内建 / 导入模块函数 / 外部库方法 / 返回 |
| 容器 | `BUILD_LIST n` / `BUILD_MAP n` / `BUILD_SET n` / `UNPACK n` / `INDEX` / `STORE_INDEX` / `GET_FIELD i` / `LENGTH` | 容器、字段与多变量解构（`变量 a, b = …`）；`STORE_INDEX` 是 `xs[i] = v` / `m[k] = v` |
| 结构体 | `STRUCT_NEW {type,fields,argc}` | 本地结构体构造；字段名保留原文，打印顺序取声明顺序 |
| 异常 | `TRY_BEGIN {target,type}` / `TRY_END` / `RETHROW` | 捕获 `VmError`，恢复栈深并把错误值交给 `捕获 (错)`；`type` 为空抓所有；`最后` 由 `TRY_BEGIN` 兜底 + `RETHROW` 原样抛回 |
| 匹配 | `EQ` + `JUMP_IF_FALSE` / `MATCH_LIST n` | 逐臂比较；`_` 臂无条件执行；列表模式先验类型与长度，再逐项比较/绑定；嵌套列表递归发检查；`否则 如果 条件` 归一成 `_` + 守卫 |
| 迭代 | `GET_ITER` / `ITER_NEXT n` | `循环…在`；耗尽时弹掉迭代器并跳到 n |
| 栈 | `POP` | 表达式语句丢弃返回值 |
| 函数值 | `LOAD_FUNC {name}` | 把本模块函数取成函数引用（用于 `按键排序(xs, 键函数)` 与 `调用(["函数名"], …)`） |
| 异步 | `TASK {kind,name,argc}` / `AWAIT` / `REGISTER_EVENT {event,name}` | 建任务句柄并入队 / 推进到该任务完成并取值 / 把回调函数挂到 `finish` 事件；任务按创建顺序推进，不是真并发 |
| 上下文 | `WITH_ENTER` | `使用 结果对象 作为 名 { … }`：失败结果直接报错，成功把值绑定到名字 |

尚未实现（M2 剩余）：`MAKE_OK` / `MAKE_ERR` / `UNWRAP` 这类结果对象专用指令；
当前结果对象由内建 `成功/失败` 构造、`取到/失败原因` 解包，语义已覆盖，暂不单开指令。
`MAKE_CLOSURE` 不需要：AGL 源语法没有闭包 / lambda / 函数值，导入器已把 Python 的
嵌套函数提升成「模块级函数 + 捕获变量变显式参数」。

运行期错误码：`AGL-RT-0001` 通用 / `AGL-RT-0010` 除零 / `AGL-RT-0020` 下标 /
`AGL-RT-0021` 键或字段 / `AGL-RT-0070` 外部库找不到 / `AGL-RT-0071` 外部库没这个方法 /
`AGL-RT-0072` FFI 调用出错 / `AGL-RT-0091` 步数配额 / `AGL-RT-0092` 效应未声明或未授权。

> 原则：**能进字节码的必须是 IR 里已经有的语义**。IR 没有的能力不先造指令，
> 否则 VM 会变成第二套语言。

## 3. 帧与运行时值（M1 设计）

- 帧：`locals`（名字 → 值）、`stack`（求值栈）、`pc`、`return_pc`、`line`；
- 值：整数 / 浮数 / 文本 / 真假 / 空 / 列表 / 映射表 / 集合 / 结构体 / 结果 / 函数引用 / 任务句柄；
- 结构体：`{类型名, 字段字典}`，字段名保留原文；
- 错误：`{错误码, AGL 行号, 消息, 调用栈}`，**不允许把 Python 异常直接漏给用户**。

## 4. 与现有后端的关系

```
                 ┌─ backend_py ─→ .py ─→ CPython（仅 --backend py 显式兜底）
AGL 源码 → IR ───┤
                 └─ bytecode ─→ .agb ─→ VM（默认；`run` 与 `build` 都走这条）
```

两个后端吃同一份 IR、共用同一套诊断；`aglc doctor` 的「AGB 双后端一致」
逐字对拍已覆盖子集，任何一边偏了都红（当前 53/53）；另有「AGB 产物与默认 VM」
断言 build 不再产出 `.py`、`run` 默认 VM。

## 5. M0 验收（已过）

1. `aglc dis` 对纯计算程序（字面量 / 算术 / 分支 / 循环 / 函数调用）输出带行号指令流；
2. `aglc dis --json` 输出可 `decode()` 的 AGB；
3. 超出已覆盖子集（真正的 AGL 范围外节点）**明确报“不在范围”**，不生成半成品；
4. 不引入任何第三方依赖。

## 6. 自举实现

`自举/` 目录存放用 AGL 自己写的编译器组件：

| 组件 | 文件 | 作用 |
|---|---|---|
| 词法分析 | `自举/词法器.agl` | 读取 `.agl` 源码，生成记号表 |
| 语法分析 | `自举/语法分析器.agl` | 递归下降生成 AST |
| 中间表示 | `自举/中间表示.agl` | AST → IR |
| 字节码生成 | `自举/字节码.agl` | IR → AGB |
| 整链入口 | `自举/自举编译器.agl` | 串起词法、语法、IR、字节码 |

自举编译器在 AGB/VM 上运行；`aglc bootstrap` 完成固定点安装，`aglc build/run` 默认使用自举编译器。

