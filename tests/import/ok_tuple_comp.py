# 期望输出: 与原 Python 一致
def main() -> None:
    xs = [(i, j) for i in range(3) for j in range(3)]
    print(xs[0][0], xs[0][1])
    print(xs[1][0], xs[1][1])


if __name__ == "__main__":
    main()
