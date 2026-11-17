# 显式类名 super：super(B, self).m() 从 B 在 MRO 中的下一位开始找方法。
# MRO=[D,C,B,A]，从 B 之后找 → A.m；这与裸 super()（从当前类 D 的下一位 C 找）不同。
class A:
    def m(self):
        return 1


class B(A):
    def m(self):
        return 10


class C(B):
    def m(self):
        return 100


class D(C):
    def m(self):
        return super(B, self).m()


def main():
    d = D()
    print(d.m())


if __name__ == "__main__":
    main()
