# 语言：中文
# 语义：列表方法族（pop/remove/insert/index/reverse，含表达式位置 pop 提升）+
#       映射方法族（update/pop 带默认，值语义重绑）+
#       格式说明进制（x/X/o/O/b/B 带 # 前缀）与对齐（>/</^）逐字对齐
def main():
    xs = [10, 20, 30]
    v = xs.pop()
    print(v, xs)
    v2 = xs.pop(0)
    print(v2, xs)
    xs.remove(20)
    print(xs)
    xs.insert(0, 99)
    print(xs)
    i = xs.index(99)
    print(i)
    xs.reverse()
    print(xs)
    d = {"a": 1, "b": 2}
    d.update({"c": 3})
    print(d["c"], d["a"], len(d))
    v4 = d.pop("z", 9)
    print(v4, d["a"], len(d))
    ys = [1, 2, 3]
    print(ys.pop(), ys)
    print(f"{255:x} {255:X} {255:o} {255:b} {255:#x}")
    print(f"{255:#b} {255:#o}")
    print(f"{7:>5} {7:<5} {7:^5}")
    print(f"{42:5d}")
    print("{:x}".format(255))
    print("{:>6x}".format(255))


if __name__ == "__main__":
    main()
