# 期望输出: 与原 Python 一致
# 特性: print() 零参数（打空行）+ pass 空函数
def nothing() -> None:
    pass


def main() -> None:
    nothing()
    print("before")
    print()
    print("after")
    print("line", end="")
    print("joined")


if __name__ == "__main__":
    main()
