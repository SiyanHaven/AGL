# 期望输出: 与原 Python 一致
def main() -> None:
    xs = [i * 2 for i in range(5)]
    print(xs[0], xs[1], xs[2], xs[3], xs[4])

    ys = [n for n in range(10) if n % 2 == 0]
    print(ys[0], ys[1], ys[2], ys[3], ys[4])


if __name__ == "__main__":
    main()
