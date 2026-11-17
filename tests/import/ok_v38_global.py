# 期望输出: 与原 Python 一致
# 特性: 可变全局变量（global 声明改写 + 多函数共享 + 方法/嵌套函数里改写）

g = 0

class C:
    def set(self):
        global g
        g = 3

def bump():
    global g
    g = g + 1

def main():
    global g

    def inner():
        global g
        g = 9
    c = C()
    c.set()
    print(g)
    g = 0
    bump()
    bump()
    print(g)
    inner()
    print(g)

if __name__ == "__main__":
    main()
