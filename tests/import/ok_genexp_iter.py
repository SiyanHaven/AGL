# 生成器表达式：惰性迭代 → AGL 展开成列表（非惰性），迭代行为一致
def main() -> None:
    xs = (i for i in range(3))
    for v in xs:
        print(v)


if __name__ == "__main__":
    main()
