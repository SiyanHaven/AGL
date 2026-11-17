# -*- coding: utf-8 -*-
"""关键字速查卡：把 spec/keywords.toml 压成一页，给写代码的 AI 和人看。

为什么要有这一层：
  · 中文关键字不在模型的训练分布里，靠"背表"必然出错；
  · 但一张表 + 一段能跑的样例，读一遍就能照着写，而且读的是**唯一来源**，
    不会出现"文档说 if、编译器说 如果"的两套说法。

卡片内容全部由 keywords.toml 生成，只有示例代码是写死的常量的原因：
示例必须是**真能编译通过**的程序，`aglc doctor` 会编译它（doctor 第 12 项）。
所以卡片不会教错语法 —— 教错了体检先红。
"""
from __future__ import annotations

from compiler.lexer import LANG_EN, LANG_ZH, KeywordTable

# 示例代码（中英各一份）。它们必须是合法 AGL 程序：
# doctor 会真的编译并运行，跑不通就红。
EXAMPLE = {
    LANG_ZH: '''#语言=中文

// 入口函数必须叫「主入口」，程序从这里开始
函数 主入口() {
  变量 名字 = "思衍"
  打印("你好，", 名字)

  变量 数们 = [3, 1, 2]
  打印("个数", 数们.长度)
  打印("合计", 求和(数们))

  变量 次数 = 0
  当 次数 小于 2 {
    次数 = 次数 + 1
  }
  打印("次数", 次数)

  尝试 {
    打印(1 / 0)
  } 捕获 (错) {
    打印("除零被捕获")
  }

  打印(分类(95))
  打印(分类(10))
}

// 有返回值就要在每个分支都 返回
函数 求和(数们: 列表<整数>) -> 整数 {
  变量 总 = 0
  循环 每个 在 数们 {
    总 = 总 + 每个
  }
  返回 总
}

函数 分类(分数: 整数) -> 文本 {
  如果 分数 大于等于 90 {
    返回 "优"
  } 否则 如果 分数 大于等于 60 {
    返回 "良"
  } 否则 {
    返回 "差"
  }
}

// 校验块在前、效应声明在后，顺序不限；两者都写在函数头与函数体之间
函数 读配置(路径: 文本) -> 文本
校验 {
  路径 不等于 ""
}
效应 读写
{
  返回 路径
}
''',
    LANG_EN: '''#lang=English

// the entry function must be main; execution starts here
fn main() {
  let name = "aglc"
  print("hello,", name)

  let nums = [3, 1, 2]
  print("count", nums.length)
  print("total", total(nums))

  let i = 0
  while i < 2 {
    i = i + 1
  }
  print("i", i)

  try {
    print(1 / 0)
  } catch (e) {
    print("caught division by zero")
  }

  print(grade(95))
  print(grade(10))
}

// every branch must return when the function declares a return type
fn total(nums: List<Int>) -> Int {
  let acc = 0
  for n in nums {
    acc = acc + n
  }
  return acc
}

fn grade(score: Int) -> String {
  if score >= 90 {
    return "A"
  } else if score >= 60 {
    return "B"
  } else {
    return "C"
  }
}

// check block first, effect declaration after; both sit between header and body
fn read_config(path: String) -> String
check {
  path != ""
}
effect IO
{
  return path
}
''',
}

# 分组顺序与标题（顺序即阅读顺序：先能跑，再谈别的）。
# 第三个元素是英文分支的标题 —— 速查卡必须整页一种语言，混着写读起来是断裂的。
_GROUPS = [
    ("decl", "声明", "declarations"),
    ("modifier", "修饰符", "modifiers"),
    ("stmt", "语句", "statements"),
    ("type", "类型", "types"),
    ("effect", "效应标签", "effect labels"),
    ("literal", "真假值", "booleans"),
    ("builtin", "入口与内建", "entry and built-ins"),
]


def _rows(table: KeywordTable, kind: str) -> list[tuple[str, str, int]]:
    return [(k.zh, k.en, k.stage) for k in table.keywords if k.kind == kind]


def _kw_table(table: KeywordTable) -> str:
    out: list[str] = []
    for kind, title in _GROUPS:
        rows = _rows(table, kind)
        if not rows:
            continue
        out.append(f"**{title}**\n")
        out.append("| 中文 | 英文 | 阶段 |")
        out.append("|---|---|---|")
        for zh, en, stage in rows:
            mark = "1" if stage <= 1 else f"{stage}（暂不可用，按普通名字处理）"
            out.append(f"| `{zh}` | `{en}` | {mark} |")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def _op_table(table: KeywordTable) -> str:
    out = ["| 中文 | 英文 | 类别 |", "|---|---|---|"]
    for o in table.ops:
        out.append(f"| `{o['zh']}` | `{o['en']}` | {o.get('category', '')} |")
    return "\n".join(out)


def _attr_table(table: KeywordTable) -> str:
    if not table.attrs:
        return "（无）"
    out = ["| 中文 | 英文 | 适用 | 只读 |", "|---|---|---|---|"]
    for a in table.attrs:
        out.append(f"| `.{a.zh}` | `.{a.en}` | 文本 / 列表 / 映射表 | 是 |")
    return "\n".join(out)


# 卡片正文（按分支各一份）。表格与示例由关键字表生成，这里只放人话。
_TEXT = {
    LANG_ZH: {
        "title": "AGL 关键字速查卡（中文分支）",
        "source": "生成自 `spec/keywords.toml` v{ver}，是唯一来源。改关键字请改表，不要改这张卡。",
        "rules_title": "写之前先记五条",
        "rules": [
            "第一行必须是 `{decl}`，缺了直接报错（AGL-E-0101）。",
            "整个文件只能用{self}关键字，混入{other}关键字报 AGL-E-0102。",
            "名字（函数名、变量名、参数名）用你自己的话，机器不翻译、不改写；字符串和注释同理。",
            "关键字不能当名字用；本卡标「暂不可用」的词可以当名字。",
            "块一律用 `{ }` 包起来，条件写在块前面，不需要圆括号。",
        ],
        "example": "能直接跑的最小程序",
        "kw": "关键字",
        "ops": "运算符",
        "ops_note": "中文分支用词，英文分支用符号；两边一一对应。",
        "attrs": "内置只读属性",
        "attrs_note": "只能读，不能写；不是对象的真实字段，写在 `.` 后面。",
        "builtin": "内建函数",
        "builtin_note": ("这些不用导入、不用声明，直接就能调。它们**不是关键字**："
                         "你可以拿这些名字当变量名，但别这么做；真重名时编译器优先用你自己的定义。"),
        "errors": "常见错误 → 正确写法",
        "h_zh": "中文", "h_en": "英文",
        "h_stage": "阶段", "h_cat": "类别", "h_use": "适用", "h_ro": "只读",
        "h_desc": "说明", "h_wrong": "写错了", "h_code": "报什么", "h_fix": "改成",
        "h_params": "参数", "h_fails": "出问题怎么办", "h_example": "示例",
        "h_no_fail": "直接报错",
        "no_attr": "（无）",
        "stage_later": "{n}（暂不可用，按普通名字处理）",
        "err_0602": "AGL-E-0102（用了另一分支的关键字）",
        "err_attr": "AGL-RT-0008（没有这个成员）",
        "fix": "出错时怎么办",
        "fix_note": "返回的 `fix_hints` 里有 `at`（哪一行）和 `suggest`（怎么改），照着改就行。",
        "fix_note2": "不要猜，也不要自己发明语法 —— 表里没有的写法就是没有。",
        "boot": "自举编译器（AGL 自己写的编译链）",
        "boot_note": ("`aglc bootstrap` 一次性安装 AGL 版编译器（不动点，~40 秒）；之后 "
                      "`aglc build/run` 默认就用它编译运行，编译前自动过一遍 "
                      "`aglc check` 同款静态检查，静态错误照样编译期拦截。"
                      "需要回退时显式加 `--compiler seed`；两者输出逐字一致。"),
    },
    LANG_EN: {
        "title": "AGL Keyword Card (English branch)",
        "source": "Generated from `spec/keywords.toml` v{ver} — the only source of truth. "
                  "Change the table, never this card.",
        "rules_title": "Five rules before you write",
        "rules": [
            "The first line must be `{decl}`; without it you get AGL-E-0101.",
            "Use {self} keywords only. A {other} keyword in the file is AGL-E-0102.",
            "Names (functions, variables, params) are yours: the machine never translates "
            "or rewrites them. Same for strings and comments.",
            "A keyword cannot be used as a name. Words marked unavailable below may be used as names.",
            "Blocks always use `{ }`; the condition sits before the block, no parentheses.",
        ],
        "example": "Smallest program that runs",
        "kw": "Keywords",
        "ops": "Operators",
        "ops_note": "Words in the Chinese branch, symbols in the English branch; they map one to one.",
        "attrs": "Built-in read-only attributes",
        "attrs_note": "Read-only, not real fields; written after a `.`.",
        "builtin": "Built-in functions",
        "builtin_note": ("These need no import and no declaration. They are **not keywords**: "
                         "you may use the names, but do not — your own definition always wins."),
        "errors": "Common mistakes → correct form",
        "h_zh": "Chinese", "h_en": "English",
        "h_stage": "Stage", "h_cat": "Category", "h_use": "Applies to", "h_ro": "Read-only",
        "h_desc": "Description", "h_wrong": "Wrong", "h_code": "Error", "h_fix": "Use",
        "h_params": "Params", "h_fails": "On failure", "h_example": "Example",
        "h_no_fail": "raises an error",
        "no_attr": "(none)",
        "stage_later": "{n} (not available yet; treated as a plain name)",
        "err_0602": "AGL-E-0102 (keyword from the other branch)",
        "err_attr": "AGL-RT-0008 (no such member)",
        "fix": "When it fails",
        "fix_note": "`fix_hints` carries `at` (which line) and `suggest` (what to write); follow them.",
        "fix_note2": "Do not guess and do not invent syntax — if it is not on this card, it does not exist.",
        "boot": "Self-hosted compiler (the compile chain written in AGL)",
        "boot_note": ("`aglc bootstrap` installs the AGL-written compiler once (fixed point, ~40 s); "
                      "after that `aglc build/run` uses it by default — a static check identical "
                      "to `aglc check` runs first, so bad programs are still rejected at compile "
                      "time. Pass `--compiler seed` to fall back; both produce byte-identical output."),
    },
}


def build(lang: str) -> str:
    """生成某一分支的速查卡（Markdown）。"""
    table = KeywordTable.load()
    t = _TEXT[lang]
    zh = lang == LANG_ZH
    decl = "#语言=中文" if zh else "#lang=English"
    self_word = "中文" if zh else "English"
    other_word = "英文" if zh else "Chinese"

    out: list[str] = [
        f"# {t['title']}",
        "",
        "> " + t["source"].replace("{ver}", table.version),
        "",
        f"## {t['rules_title']}",
        "",
    ]
    for i, rule in enumerate(t["rules"], 1):
        text = (rule.replace("{decl}", decl)
                .replace("{self}", self_word)
                .replace("{other}", other_word))
        out.append(f"{i}. {text}")

    out += ["", f"## {t['example']}", "", "```agl", EXAMPLE[lang].rstrip(), "```", ""]

    # ── 关键字（按 kind 分组）──────────────────────────────────────
    out += [f"## {t['kw']}", ""]
    for kind, title_zh, title_en in _GROUPS:
        rows = _rows(table, kind)
        if not rows:
            continue
        out += [f"**{title_zh if zh else title_en}**", "",
                f"| {t['h_zh']} | {t['h_en']} | {t['h_stage']} |", "|---|---|---|"]
        for kz, ke, stage in rows:
            mark = "1" if stage <= 1 else t["stage_later"].replace("{n}", str(stage))
            out.append(f"| `{kz}` | `{ke}` | {mark} |")
        out.append("")

    # ── 运算符 ────────────────────────────────────────────────────
    out += [f"## {t['ops']}", "", t["ops_note"], "",
            f"| {t['h_zh']} | {t['h_en']} | {t['h_cat']} |", "|---|---|---|"]
    for o in table.ops:
        out.append(f"| `{o['zh']}` | `{o['en']}` | {o.get('category', '')} |")
    out.append("")
    if zh:
        out.append("> 增广赋值：`+=` `-=` `*=` `/=` `%=` 均可用（例：`计数 += 1`）；"
                   "左值须是变量（或成员 / 下标），常量不能增广赋值。")
    else:
        out.append("> Augmented assignment: `+=` `-=` `*=` `/=` `%=` all work "
                   "(e.g. `count += 1`); the left side must be a variable "
                   "(or member/index), not a constant.")

    # ── 内置属性 ──────────────────────────────────────────────────
    out += [f"## {t['attrs']}", "", t["attrs_note"], ""]
    if table.attrs:
        out += [f"| {t['h_zh']} | {t['h_en']} | {t['h_use']} | {t['h_ro']} |",
                "|---|---|---|---|"]
        for a in table.attrs:
            use = "文本 / 列表 / 映射表" if zh else "text / list / map"
            yes = "是" if zh else "yes"
            out.append(f"| `.{a.zh}` | `.{a.en}` | {use} | {yes} |")
    else:
        out.append(t["no_attr"])
    out.append("")

    # ── 内建函数（整张表由 keywords.toml 生成，卡片只负责排版）─────────
    # 「会失败」那一列不能省：`转整数` 返回结果、`切片` 越界直接报错，
    # 两者处理方式完全不同，混起来写就是 AI 最容易踩的坑。
    out += [f"## {t['builtin']}", "", t["builtin_note"], "",
            f"| {t['h_zh']} | {t['h_en']} | {t['h_params']} | {t['h_fails']} | "
            f"{t['h_desc']} | {t['h_example']} |",
            "|---|---|---|---|---|---|"]
    for bi in table.builtins:
        params = " · ".join(bi.params + (("…",) if bi.rest else ()))
        fails = ("返回 `" + ("失败" if zh else "Err") + "`") if bi.may_fail else t["h_no_fail"]
        desc = bi.summary_zh if zh else bi.summary_en
        out.append(f"| `{bi.zh}` | `{bi.en}` | {params} | {fails} | {desc} | "
                   f"`{bi.example}` |")
    out.append("")

    # ── 常见错误（从表里取最常写错的几个，保证建议和编译器口径一致）──
    out += [f"## {t['errors']}", "",
            f"| {t['h_wrong']} | {t['h_code']} | {t['h_fix']} |", "|---|---|---|"]
    limits = {"stmt": 6, "decl": 4, "builtin": 2}
    seen: set[str] = set()
    for kind, limit in limits.items():
        n = 0
        for k in table.keywords:
            if k.kind != kind or k.stage > 1 or k.en in seen:
                continue
            seen.add(k.en)
            wrong = k.en if zh else k.zh
            right = k.zh if zh else k.en
            out.append(f"| `{wrong}` | {t['err_0602']} | `{right}` |")
            n += 1
            if n >= limit:
                break
    attr_wrong = "`x.大小`" if zh else "`x.size`"
    attr_right = "`x.长度`" if zh else "`x.length`"
    out.append(f"| {attr_wrong} | {t['err_attr']} | {attr_right} |")

    out += ["", f"## {t['fix']}", "", "```", "python -m compiler.cli check 文件.agl --json"
            if zh else "python -m compiler.cli check file.agl --json", "```", "",
            t["fix_note"], t["fix_note2"], ""]
    out += [f"## {t['boot']}", "", t["boot_note"], ""]
    return "\n".join(out)
