# 表达式位置的生成器实参：(i for i in ...) 出现在函数实参里，
# 被提升成前置临时列表（临时变量要带「变量」声明，否则引用处报未声明）
def total(xs):
    s = 0
    for x in xs:
        s = s + x
    return s


def main():
    print(total(i * 2 for i in range(3)))


if __name__ == "__main__":
    main()
