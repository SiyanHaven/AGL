# 期望输出: 与原 Python 一致
# 特性: 可变类属性赋值（C.x = v 改写 + 实例读共享值 + 继承链上的类属性）

class B:
    n = 1

class C(B):
    pass

class D:
    x = 1

    def bump(self):
        D.x = D.x + 1

def main():
    B.n = 5
    c = C()
    print(c.n)
    print(B.n)
    d = D()
    print(d.x)
    d.bump()
    d.bump()
    print(d.x)

if __name__ == "__main__":
    main()
