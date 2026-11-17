# sorted 带 key / reverse：key 用本文件函数名或 lambda，
# reverse 是常量真 —— 全部直译成 按键排序 / 反转，与 Python 逐字对拍。


def keyf(x):
    return x % 10


def main():
    xs = [25, 3, 18, 7]
    print(sorted(xs, key=keyf))
    print(sorted(xs, reverse=True))
    print(sorted(xs, key=lambda x: -x))
    print(sorted(xs, key=keyf, reverse=True))


if __name__ == '__main__':
    main()
