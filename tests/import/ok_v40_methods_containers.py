# 期望输出: 与原 Python 一致
# 特性: v0.40 续——普通方法任意 receiver 分发 + 容器内嵌 __repr__ + 多层属性链

class Inner:
    def greet(self):
        return "hi-inner"

class Outer:
    def __init__(self):
        self.inner = Inner()

class C:
    def greet(self):
        return "hi-from-C"
    def __repr__(self):
        return "C(repr!)"

def make():
    return C()

class Deep:
    def greet(self):
        return "hi-deep"

class Mid:
    def __init__(self):
        self.deep = Deep()

class Top:
    def __init__(self):
        self.mid = Mid()

def main():
    # 普通方法调用：直接构造 / 属性取出 / 函数返回 / 多层属性链
    print(C().greet())
    o = Outer()
    print(o.inner.greet())
    print(make().greet())
    t = Top()
    print(t.mid.deep.greet())
    # 容器内嵌实例走 __repr__
    c = C()
    print([c])
    print([c, c])
    print([1, c, "x"])
    print((c,))
    print((c, 1))
    print({"k": c})

if __name__ == "__main__":
    main()
