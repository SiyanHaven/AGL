# 语言：中文
# 语义：d.items()/d.values() 映射迭代 + 列表切片（x[a:b]/x[:b]/x[a:]/x[::-1] 反转）+
#       f-string {x:05d} 整数补零 + % 格式化 → 填充 + str.format → 填充 +
#       内建透传修复 round/pow/divmod/ord/chr/all/any/hex/oct/bin
def main():
    d = {"a": 1, "b": 2}
    for k, v in d.items():
        print(k, v)
    vs = d.values()
    print(len(list(vs)))
    xs = [10, 20, 30, 40]
    print(xs[1:3])
    print(xs[:2])
    print(xs[2:])
    print(xs[::-1])
    print(xs[-1])
    print("%s-%d" % ("x", 5))
    pi = 3.14159
    print(f"{pi:.2f}")
    print(f"{42:05d}")
    print(round(3.7))
    print(pow(2, 10))
    print(divmod(17, 5)[0])
    print(divmod(17, 5)[1])
    print(ord("A"))
    print(chr(66))
    print(all([True, True]))
    print(any([False, True]))
    print(hex(255))
    print(oct(8))
    print(bin(10))
    print("{}-{}".format(7, 8))


if __name__ == "__main__":
    main()
