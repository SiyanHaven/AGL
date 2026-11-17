# 期望输出: 与原 Python 一致
# 特性: 嵌套函数提升成模块级函数 + 捕获变量变显式参数（调用处补传）
def main() -> None:
    # 无捕获
    def double(x: int) -> int:
        return x * 2
    print(double(21))
    # 捕获外层参数
    base = 10
    def add_base(y: int) -> int:
        return base + y
    print(add_base(5))
    # 捕获外层局部 + 多层嵌套
    a = 1
    def mid() -> int:
        b = 10
        def inner(y: int) -> int:
            return a + b + y
        return inner(100)
    print(mid())
    # 捕获在调用点取当前值（Python 闭包读的是调用时的值）
    step = 1
    def scale(v: int) -> int:
        return v * step
    print(scale(3))
    step = 2
    print(scale(3))


if __name__ == "__main__":
    main()
