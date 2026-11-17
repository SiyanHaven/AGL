# 期望输出: 与原 Python 一致
# 特性: 星号解包 a, *rest = ...（头部/中部/尾部三种形态）
def main() -> None:
    head, *mid, tail = [1, 2, 3, 4, 5]
    print(head, mid, tail)

    a, *rest = [1, 2, 3, 4]
    print(a, rest)

    *front, last = [1, 2, 3]
    print(front, last)


if __name__ == "__main__":
    main()
