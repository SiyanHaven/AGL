# enumerate / zip 解包循环：展开成计数式 当 循环 + 下标，
# 与 Python 逐字对拍（enumerate 的索引、start 参数、zip 取最短、普通元组列表）。


def main():
    words = ['甲', '乙', '丙']
    for i, w in enumerate(words):
        print(i, w)
    for i, w in enumerate(words, start=1):
        print(i, w)
    for a, b in zip([1, 2, 3], ['x', 'y']):
        print(a, b)
    for x, y in [(1, 2), (3, 4)]:
        print(x + y)


if __name__ == '__main__':
    main()
