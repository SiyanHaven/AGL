# 语言：中文
# 语义：文本乘法（s * n）/ .title / .zfill / .lstrip / .rstrip / 映射表按键迭代 for k in d
def main():
    d = {"a": 1, "b": 2, "c": 3}
    total = 0
    for k in d:
        total = total + d[k]
    print(total)
    for k in d:
        print(k)
    s = "ab" * 3
    print(s)
    print("hello world".title())
    print(str(7).zfill(3))
    print("  甲".lstrip())
    print("甲  ".rstrip())
    print(3 * "x")


if __name__ == "__main__":
    main()
