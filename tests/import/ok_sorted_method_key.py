# sorted 的 key 是 str.lower / str.upper / str.strip：AGL 有对应内建，
# 提升成无闭包键函数（_键N(x) { 返回 转小写(x) }），逐字对拍。


def main():
    ys = sorted(['B', 'a'], key=str.lower)
    for x in ys:
        print(x)
    us = sorted(['bb', 'A', 'ccc'], key=str.upper)
    for x in us:
        print(x)
    ts = sorted(['  a ', 'b'], key=str.strip)
    print(len(ts))


if __name__ == '__main__':
    main()