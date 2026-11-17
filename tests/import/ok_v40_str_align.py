# 期望输出: 与原 Python 一致
# 特性: __str__/__repr__ 全对齐——属性取出/函数返回/直接构造/子类继承/!r

class Inner:
    def __str__(self):
        return "Inner!"

class Outer:
    def __init__(self):
        self.inner = Inner()

class C:
    def __str__(self):
        return "C-from-maker"

def make():
    return C()

class Base:
    def __str__(self):
        return "Base-str"

class Sub(Base):
    def __init__(self):
        self.x = 1

class R:
    def __repr__(self):
        return "R(repr!)"

def main():
    o = Outer()
    print(o.inner)
    print(str(o.inner))
    print(f"[{o.inner}]")
    print(make())
    print(Sub())
    print(str(Sub()))
    print(f"<{Sub()}>")
    r = R()
    print(repr(r))
    print(f"{r!r}")

if __name__ == "__main__":
    main()
