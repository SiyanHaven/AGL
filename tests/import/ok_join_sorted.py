# join / sorted / super 关键字实参：导入器补齐的最后三个登记缺口
class Base:
    def __init__(self, a, b):
        self.a = a
        self.b = b

    def label(self, tag, sep) -> str:
        return tag + sep + str(self.a)


class Child(Base):
    def __init__(self, a, b):
        super().__init__(b=b, a=a)        # 关键字实参乱序 → 按形参绑定

    def label(self, tag, sep) -> str:
        return super().label(sep=sep, tag=tag)   # super().m 关键字实参


def main() -> None:
    parts = ["甲", "乙", "丙"]
    print("-".join(parts))                # 字符串 join → 拼接列表
    xs = [3, 1, 2]
    print(sorted(xs))                     # sorted → 排序
    print(xs)                             # 排序不修改原列表
    c = Child(1, 2)
    print(c.a)
    print(c.b)
    print(c.label("x", "-"))


if __name__ == "__main__":
    main()
