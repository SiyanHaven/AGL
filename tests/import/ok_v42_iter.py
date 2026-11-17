# v0.42：自定义对象迭代器 __next__ 方法分发
# 注意：AGL 约定 __next__ 返回空表示结束（没有 StopIteration 异常），
# 所以对拍用例用手动 while 循环调用 __next__，不用 for x in obj（Python 的 for 只认 StopIteration）。

class Counter:
    def __init__(self, n):
        self.n = n
        self.i = 0
    def __next__(self):
        if self.i >= self.n:
            return None
        v = self.i
        self.i += 1
        return v

def main():
    c = Counter(3)
    total = 0
    while True:
        x = c.__next__()
        if x is None:
            break
        total = total + x
    print(total)


if __name__ == '__main__':
    main()
