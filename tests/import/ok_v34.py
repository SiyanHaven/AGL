# 语言：中文
# 语义：v0.34——切片赋值（重绑拼接）· endswith/startswith 元组 · enumerate 位置 start ·
#       casefold/expandtabs/rpartition · 只读类属性
class Config:
    version = 3


def main():
    xs = [1, 2, 3, 4]
    xs[1:3] = [9]
    print(len(xs))
    print(xs[0])
    print(xs[1])
    print(xs[2])
    s = "a.py"
    print(s.endswith((".py", ".txt")))
    print(s.endswith((".c", ".h")))
    print(s.startswith(("a", "b")))
    total = 0
    for i, v in enumerate(["x", "y"], 1):
        total = total + i * 2
    print(total)
    print("Ä".casefold())
    print(len("a\tb".expandtabs(4)))
    print("a-b-c".rpartition("-")[0])
    print("a-b-c".rpartition("-")[1])
    print("abc".rpartition("-")[0])
    print("abc".rpartition("-")[2])
    print(Config.version)


if __name__ == "__main__":
    main()
