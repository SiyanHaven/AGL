# 语言：中文
# 语义：d.get 方法 / input() / 字符串方法族 count-find-isdigit-isalpha / xs.sort() /
#       min-max 多参与列表 / sum 带 start / range 当值 + list(range) /
#       字典 ** 展开 / 集合推导
def main():
    d = {"a": 1, "b": 2}
    print(d.get("a"))
    print(d.get("z", 9))
    s = "香蕉香蕉苹果"
    print(s.count("香蕉"))
    print(s.find("苹果"))
    print("123".isdigit())
    print("abc".isalpha())
    xs = [3, 1, 2]
    xs.sort()
    print(xs)
    ys = [3, 1, 2]
    ys.sort(reverse=True)
    print(ys)
    print(min(3, 1, 2))
    print(max([3, 1, 2]))
    print(sum([1, 2, 3], 10))
    print(list(range(5)))
    r = range(2, 8, 2)
    print(list(r))
    m = {"a": 1}
    n = {**m, "b": 2}
    print(n["b"])
    o = {"x": 0, **m}
    print(o["a"])
    print(len({**m}))
    nums = {x * 2 for x in range(4)}
    print(sorted(nums))


if __name__ == "__main__":
    main()
