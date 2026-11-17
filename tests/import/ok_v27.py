# 语言：中文
# 语义：剩余字符串方法 ljust/rjust/center/removeprefix/removesuffix/swapcase/capitalize/
#       islower/isupper/isspace/istitle/isdecimal/isnumeric + f-string 内 join +
#       enumerate start=2 通用性 + list(d)/d.keys() 按键列表展开 +
#       bool()/list()/dict()/set()/tuple() 类型构造
def main():
    s = "abc"
    print(s.ljust(5, "-"))
    print(s.rjust(5, "-"))
    print(s.center(7, "*"))
    print("AbC".swapcase())
    print("abc".capitalize())
    print("abc".removeprefix("a"))
    print("abc".removesuffix("c"))
    print("abc".islower())
    print("ABC".isupper())
    print("  ".isspace())
    print("Hello World".istitle())
    print("42".isdecimal())
    print("42".isnumeric())
    xs = ["a", "b", "c"]
    print(f"{'-'.join(xs)}")
    print(f"[{', '.join(xs)}]")
    ys = [10, 20, 30]
    for i, v in enumerate(ys, start=2):
        print(i, v)
    d = {"x": 1, "y": 2}
    ks = list(d)
    print(len(ks))
    print(ks[0])
    print(ks[1])
    ks2 = list(d.keys())
    print(len(ks2))
    print(bool(0))
    print(bool([1]))
    print(bool(""))
    ls = list("abc")
    print(ls[0])
    print(ls[2])
    m = dict([("a", 1), ("b", 2)])
    print(m["a"])
    print(m["b"])
    print(len(dict()))
    st = set([1, 2, 2])
    print(len(st))
    t = tuple([1, 2])
    print(t[0])
    print(t[1])


if __name__ == "__main__":
    main()
