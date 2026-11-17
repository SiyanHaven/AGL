# 期望输出: 与原 Python 一致
# 特性: max/min 带 default=（空列表兜底）+ pow 三参
def main() -> None:
    xs: list[int] = []
    print(max(xs, default=0), min(xs, default=-1))

    ys = [1, 5, 3]
    print(max(ys, default=0), min(ys, default=0))

    print(pow(7, 3, 10))
    print(pow(2, 10))


if __name__ == "__main__":
    main()
