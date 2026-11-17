# 语言：中文
# 语义：v0.35——strip 族带字符集 · isinstance · print end/sep · assert · del 映射/列表 · reversed(range)
class Cfg:
    tag = "v35"


def main():
    s = "  a b  "
    s = s.strip()
    print(s)
    print(s.lstrip("a"))
    print("0012".zfill(6))
    print("hello world".title())
    print(isinstance(5, int))
    print(isinstance("s", (str, list)))
    print(isinstance(3.5, float))
    print("a", end="")
    print("b")
    print("x", "y", sep="-")
    x = 3
    assert x > 1
    assert x < 10, "x too big"
    print("assert-ok")
    d = {"a": 1, "b": 2}
    del d["a"]
    print(len(d))
    print("b" in d)
    xs = [0, 1, 2, 3]
    del xs[1]
    print(len(xs))
    print(xs[0])
    print(xs[2])
    t = 0
    for i in reversed(range(3)):
        t = t + i
    print(t)
    print(Cfg.tag)


if __name__ == "__main__":
    main()
