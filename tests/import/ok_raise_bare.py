# except 块里的裸 raise（重抛当前异常）翻译成 取到(失败(失败原因(错)))，
# 外层 尝试/捕获 接住，与 Python 的 raise + 外层 except 逐字对拍。


def main():
    try:
        try:
            raise ValueError('inner')
        except Exception:
            print('re-raise')
            raise
    except Exception:
        print('outer-caught')


if __name__ == '__main__':
    main()