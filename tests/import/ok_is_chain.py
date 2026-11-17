# 期望输出: 与原 Python 一致
# 特性: 链式 `is` / `is not`（中间值是纯读取）→ 等同/不等同 分段合取
def main() -> None:
    a = [1]
    b = a
    c = a
    print(a is b is c)
    c2 = [1]
    print(a is b is c2)
    print(a is b is not c2)
    print(a is not c2 is not b)
    # 常量中间值（小整数缓存语义与 Python 一致）
    n = 1
    print(n is 1 is 1)
    print(n is 2 is 1)
    # 与判空链混合
    xs = None
    print(xs is None is None)
    print(xs is None is not None)


if __name__ == "__main__":
    main()
