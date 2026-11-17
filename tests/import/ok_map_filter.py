# map / filter 内联进 循环：与惰性迭代器单次消费完全等价，副作用顺序一致。


def twice(x):
    return x * 2


def big(x):
    return x > 2


def main():
    for y in map(twice, [1, 2, 3]):
        print(y)
    for y in filter(big, [1, 2, 3, 4]):
        print(y)
    for y in map(lambda x: x + 1, [5, 6]):
        print(y)


if __name__ == '__main__':
    main()
