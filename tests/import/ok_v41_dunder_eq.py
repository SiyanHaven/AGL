# 期望输出: 与原 Python 一致
# 特性: v0.41——对象协议 dunder 运算符重载（__eq__/__getitem__/__len__/__add__/__call__）

class EqC:
    def __init__(self, v):
        self.v = v
    def __eq__(self, other):
        return self.v == other.v
    def __repr__(self):
        return f"EqC({self.v})"

class Bag:
    def __init__(self):
        self.data = [10, 20, 30]
    def __getitem__(self, i):
        return self.data[i]

class Counter:
    def __init__(self, n):
        self.n = n
    def __len__(self):
        return self.n

class Vec:
    def __init__(self, x):
        self.x = x
    def __add__(self, other):
        return Vec(self.x + other.x)
    def __repr__(self):
        return f"Vec({self.x})"

class Adder:
    def __init__(self, n):
        self.n = n
    def __call__(self, x):
        return self.n + x

def main():
    # __eq__
    a = EqC(1)
    b = EqC(1)
    c = EqC(2)
    print(a == b)
    print(a == c)
    # __getitem__
    bag = Bag()
    print(bag[0])
    print(bag[2])
    # __len__
    print(len(Counter(5)))
    # __add__
    print(Vec(1) + Vec(2))
    # __call__
    add5 = Adder(5)
    print(add5(3))

if __name__ == "__main__":
    main()
