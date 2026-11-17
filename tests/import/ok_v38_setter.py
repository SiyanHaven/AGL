# 期望输出: 与原 Python 一致
# 特性: @property setter（@x.setter 完整链路 + 方法内赋值）

class C:
    def __init__(self):
        self._x = 1

    @property
    def x(self):
        return self._x

    @x.setter
    def x(self, v):
        self._x = v

    def dou(self):
        self.x = self.x + 1

def main():
    c = C()
    c.x = 5
    print(c.x)
    c.x = c.x + 1
    print(c.x)
    c.dou()
    print(c.x)

if __name__ == "__main__":
    main()
