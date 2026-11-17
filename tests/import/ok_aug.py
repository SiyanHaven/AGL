# 期望输出: 与原 Python 一致
def main() -> None:
    total = 0
    for i in range(0, 4):
        total += i
    print(total)
    n = 5
    n *= 3
    n -= 1
    print(n)


if __name__ == "__main__":
    main()
