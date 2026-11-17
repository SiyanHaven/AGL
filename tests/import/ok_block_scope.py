# 块内首次声明提升：Python 无块作用域，AGL 有。
# 覆盖 try/except 双分支兜底、finally 引用 except 块内变量、
# if/else 双分支赋值后块外引用、循环内声明后循环外引用。


def fallback(v):
    try:
        x = v[0]
    except Exception:
        x = 0
    return x


def fin():
    try:
        z = 1 / 0
    except ZeroDivisionError:
        m = 'handled'
    finally:
        print(m)
    print('done')


def pick(flag):
    if flag:
        y = 'pos'
    else:
        y = 'neg'
    return y


def main():
    print(fallback([7]))
    print(fallback([]))
    print(pick(True))
    print(pick(False))
    fin()
    i = 0
    while i < 3:
        i = i + 1
    print(i)


if __name__ == '__main__':
    main()
