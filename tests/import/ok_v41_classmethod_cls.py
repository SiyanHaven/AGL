# 期望输出: 与原 Python 一致
# 特性: v0.41——classmethod 里 cls 参数正确解析（cls.属性 / cls.方法 / cls.x += 1）

class Foo:
    count = 0

    @classmethod
    def bump(cls):
        cls.count += 1
        return cls.count

    @classmethod
    def describe(cls):
        return f"Foo has count={cls.count}"

def main():
    print(Foo.bump())
    print(Foo.bump())
    print(Foo.describe())

if __name__ == "__main__":
    main()
