# 语言：中文
# 语义：% 格式化带修饰（%05d/%.2f/%Ns）逐字对齐 + 负步长/正步长/动态步长切片 +
#       str.format 命名/下标/格式占位符 + format(x, spec) + int()/float() 构造
def main():
    print("%05d" % 42)
    print("%.2f" % 3.14159)
    print("%10s" % "ab")
    print("%-5s" % "ab")
    print("%s-%05d" % ("x", 42))
    xs = [10, 20, 30, 40, 50]
    print(xs[3:0:-1])
    print(xs[:2:-1])
    print(xs[2::-1])
    print(xs[::2])
    n = 2
    print(xs[1:4:n])
    print("{1}-{0}".format(7, 8))
    print("{name}".format(name="x"))
    print("{b} {a}".format(a=1, b=2))
    print("{a}:{b:05d}".format(a="x", b=42))
    print(format(42, "05d"))
    print(format(3.14, ".2f"))
    print(int("42"))
    print(int(3.7))
    print(int("101", 2))
    print(float("3.14"))
    print(int())
    print(float())


if __name__ == "__main__":
    main()
