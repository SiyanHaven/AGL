# 期望输出: 与原 Python 一致
def main() -> None:
    x = 3.14159
    print(f"圆周率约等于 {x:10.2f}")
    y = 2.71828
    print(f"自然常数 {y:8.1f}")
    z = 1.0
    print(f"整数形态 {z:.0f}")


if __name__ == "__main__":
    main()
