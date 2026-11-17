# lambda 调用本文件的普通函数：函数名是文件级全局名字，
# AGL 里是可直接调用的全局函数，不算闭包捕获（不该被误拒）。
def square(x):
    return x * x


def main():
    f = lambda x: square(x) + 1
    print(f(3))


if __name__ == "__main__":
    main()
