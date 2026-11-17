# 多重继承：C3 线性化方法解析；同名方法按 MRO 取最先定义者；super() 沿 MRO；
# __init__ 协作链（super().__init__ 透传）展开到同一个实例映射表上
class A:
    def x(self) -> int:
        return 1

    def y(self) -> str:
        return "A-y"


class B:
    def x(self) -> int:
        return 2

    def y(self) -> str:
        return "B-y"


class C(A, B):
    def x(self) -> int:
        return 10 + super().x()


class D(A, B):
    pass


class MixA:
    def __init__(self, a, b):
        self.a = a
        super().__init__(b)


class MixB:
    def __init__(self, b):
        self.b = b


class MixC(MixA, MixB):
    def __init__(self, a, b):
        super().__init__(a, b)


def main() -> None:
    c = C()
    print(c.x())          # 覆盖后 super().x() 沿 MRO 取 A.x → 11
    print(c.y())          # 未覆盖 → MRO 先 A.y → "A-y"
    d = D()
    print(d.x())          # 未覆盖 → MRO 先 A.x → 1
    print(d.y())          # 未覆盖 → A.y → "A-y"
    m = MixC(1, 2)
    print(m.a)
    print(m.b)


if __name__ == "__main__":
    main()
