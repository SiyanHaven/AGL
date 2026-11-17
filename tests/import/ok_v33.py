# 语言：中文
# 语义：v0.33——切分 maxsplit、替换 count、元素位置 文本版、右查找、
#       默认键、从键构建、整除、幂赋值、列表乘法
def main():
    s = "a b c d"
    parts = s.split(" ", 2)
    print(len(parts))
    print(parts[0])
    print(parts[2])
    print("a-a-a-a".replace("a", "b", 2))
    print("abc".index("b"))
    print("abcabc".rfind("b"))
    print("abc".rfind("z"))
    d = {"a": 1}
    v = d.setdefault("b", 2)
    print(v)
    print(len(d))
    print(d["b"])
    ks = ["x", "y"]
    m = dict.fromkeys(ks, 0)
    print(len(m))
    print(m["x"])
    print(m["y"])
    print(7 // 2)
    print(-7 // 2)
    n = 2
    n **= 3
    print(n)
    z = [0] * 3
    print(len(z))
    print(z[0])
    print(z[2])


if __name__ == "__main__":
    main()
