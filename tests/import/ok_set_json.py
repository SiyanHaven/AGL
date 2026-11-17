# 语言：中文
# 语义：集合字面量/集合内建 + json 内建 + @staticmethod / @classmethod
import json


class Counter:
    def __init__(self, start):
        self.value = start

    def inc(self, n):
        self.value = self.value + n
        return self.value

    @staticmethod
    def double(x):
        return x * 2

    @classmethod
    def unit(cls, x):
        return x + 1


def main():
    s = {1, 2, 2, 3}
    print(len(s))
    if 3 in s:
        print("yes")
    else:
        print("no")
    t = s | {3, 4}
    print(sorted(t))
    u = s & {2, 3}
    print(sorted(u))
    v = s - {2}
    print(sorted(v))
    w = s ^ {1, 9}
    print(sorted(w))
    print(s <= t)
    print(t >= s)
    s.add(9)
    print(len(s))
    d = json.loads('{"a": 1, "b": [2, 3]}')
    print(d["a"])
    print(len(d["b"]))
    print(json.dumps({"x": 1}))
    c = Counter(10)
    print(c.inc(5))
    print(Counter.double(4))
    print(c.double(6))
    print(Counter.unit(7))


if __name__ == "__main__":
    main()
