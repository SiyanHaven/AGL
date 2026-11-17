# 期望输出: 与原 Python 一致
def main() -> None:
    f = lambda x: x + 1
    print(f(2))
    g = lambda a, b: a * b
    print(g(3, 4))


if __name__ == "__main__":
    main()
