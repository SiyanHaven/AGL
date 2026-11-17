# 期望输出: 与原 Python 一致
# 特性: 带步长切片赋值 xs[a:b:c] = v（正步长交错覆盖）
def main() -> None:
    xs = [0, 1, 2, 3, 4, 5]
    xs[0:6:2] = [9, 9, 9]
    print(xs)

    ys = [0, 1, 2, 3, 4, 5]
    ys[1:6:3] = [8, 8]
    print(ys)

    zs = list(range(10))
    zs[::3] = [7, 7, 7, 7]
    print(zs)


if __name__ == "__main__":
    main()
