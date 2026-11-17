# 期望输出: 与原 Python 一致
# 特性: `is` / `is not` 非判空同一性 → 等同 / 不等同（判空保持 等于(空)）
def main() -> None:
    a = [1, 2]
    b = a
    c = [1, 2]
    print(a is b, a is c, a is not c)
    print(b is a, c is not a)
    # 判空形态回归：仍是判空翻译
    xs = None
    ys = [1]
    print(xs is None, ys is None, ys is not None)
    # 可变值引用同一性：同源列表 is 真、字面量重建 is 假
    m = {"k": [1]}
    n = m
    print(m is n, m is {"k": [1]})


if __name__ == "__main__":
    main()
