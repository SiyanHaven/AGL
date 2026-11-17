# 期望输出: 与原 Python 一致
计数 = 0


def next_count() -> int:
    return 计数 + 1


def main() -> None:
    print(next_count())
    print(计数)


if __name__ == "__main__":
    main()
