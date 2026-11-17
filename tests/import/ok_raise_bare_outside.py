# 块外的裸 raise：Python 运行时抛 RuntimeError（没有活动异常可重抛），
# 翻译成 取到(失败("没有活动异常可重抛"))，可被 尝试/捕获 接住，逐字对拍。


def main():
    try:
        raise
    except Exception:
        print('caught')


if __name__ == '__main__':
    main()