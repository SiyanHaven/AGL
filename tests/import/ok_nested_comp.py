# 期望输出: 与原 Python 一致
def main() -> None:
    xs = [i * j for i in range(3) for j in range(3)]
    print(xs[0], xs[1], xs[2], xs[3], xs[4], xs[5], xs[6], xs[7], xs[8])


if __name__ == "__main__":
    main()
