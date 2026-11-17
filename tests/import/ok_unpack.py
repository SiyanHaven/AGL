# 解包赋值直译：`a, b = ...` 首次出现生成 `变量 a, b = ...`（声明式解包），
# 已声明的生成重赋值；元组值翻译成列表（挂 warning）；与 Python 逐字对拍。


def main():
    a, b = (1, 2)
    print(a, b)
    c, d = [3, 4]
    print(c, d)
    e, f = "xy"
    print(e, f)
    x, y = 1, 2
    print(x, y)
    a, b = b, a
    print(a, b)
    g, h, i = [7, 8, 9]
    print(g, h, i)


if __name__ == '__main__':
    main()