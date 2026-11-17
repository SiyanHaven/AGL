# 期望输出: 与原 Python 一致
# 特性: __str__（print(c) / str(c) / f-string 内嵌实例走 __str__）

class C:
    def __init__(self):
        self.v = 7

    def __str__(self):
        return "v=" + str(self.v)

def main():
    c = C()
    print(c)
    print(str(c))
    print(c.v, c)
    print(f"{c}!")

if __name__ == "__main__":
    main()
