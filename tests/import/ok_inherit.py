# 继承：父类已定义；子类继承父类字段，super().__init__ 链式初始化；覆盖一个方法，转发一个方法
class Bar:
    def __init__(self):
        self.base = 10

    def get_base(self) -> int:
        return self.base


class Foo(Bar):
    def __init__(self):
        super().__init__()
        self.extra = 5

    def get_total(self) -> int:
        return self.get_base() + self.extra


def main() -> None:
    f = Foo()
    print(f.get_total())
    print(f.get_base())


if __name__ == "__main__":
    main()
