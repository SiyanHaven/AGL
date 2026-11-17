# 期望输出: 与原 Python 一致
def main() -> None:
    s = {i for i in range(3)}
    print(len(s))
    t = {x % 2 for x in range(6)}
    print(len(t))
    u = {x * 2 for x in range(4)}
    print(len(u))


if __name__ == "__main__":
    main()
