# raise ... from ... 的原因并入失败消息（AGL 没有异常链语义，挂警告），
# 外层捕获对拍一致。


def main():
    try:
        raise ValueError('x') from TypeError('y')
    except Exception:
        print('caught-from')


if __name__ == '__main__':
    main()