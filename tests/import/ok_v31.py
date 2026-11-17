# 语言：中文
# 语义：clear 歧义（列表/映射运行时按类型清空）+ 泛型 pop（未登记 1 参
#       按实参类型分发）+ 复杂格式（`=`/自定义填充/符号/千分位/科学计数，
#       按格式 原生透传）+ %% 转义 + **d 展开 + f-string !r + 嵌套宽度 +
#       动态步长缺省端切片 + 科学计数法字面量
def main():
    xs = [1, 2, 3]
    xs.clear()
    print(xs)
    d = {"a": 1, "b": 2}
    d.clear()
    print(len(d))
    ys = [10, 20, 30]
    y = ys.pop(1)
    print(y, ys)
    md = {"x": 5, "y": 6}
    mv = md.pop("x")
    print(mv, md["y"], len(md))
    print(f"{42:=10}")
    print(f"{42:=010}")
    print(f"{42:+d} {-42:+d} {42: d}")
    print(f"{1234567:,}")
    print(f"{3.14159:.3e} {0.0000123:g}")
    print(f"{42:*<10} {42:*>10} {42:*^10}")
    print("100%% %s" % "ok")
    base = 2
    print(int("101", base))
    print("{a} {b}".format(**{"a": 1, "b": 2}))
    x = "xy"
    print(f"{42!r} {x!r}")
    w = 5
    print(f"{42:{w}}")
    print(f"{42:>{w}}")
    xs2 = [1, 2, 3, 4, 5]
    n = 2
    print(xs2[:n:])
    print(xs2[:n:-1])
    print(xs2[::n])
    print(xs2[n::])


if __name__ == "__main__":
    main()
