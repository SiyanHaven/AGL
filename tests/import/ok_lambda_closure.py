# lambda 闭包捕获：n 被捕获 → 提升成显式参数，调用处自动补传
def main() -> None:
    n = 5
    f = lambda x: x + n
    print(f(1))


if __name__ == "__main__":
    main()
