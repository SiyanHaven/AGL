# 期望输出: 与原 Python 一致
# 特性: 动态步长切片赋值（变量步长，正/负/显式起止）

def main():
    xs = [1, 2, 3, 4, 5]
    c = 2
    xs[::c] = [9, 8, 7]
    print(xs)
    ys = [1, 2, 3, 4, 5]
    c = -2
    ys[::c] = [9, 8, 7]
    print(ys)
    zs = [0, 1, 2, 3, 4]
    c = -1
    zs[4:1:c] = [7, 6, 5]
    print(zs)

if __name__ == "__main__":
    main()
