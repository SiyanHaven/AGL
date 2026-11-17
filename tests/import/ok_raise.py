# raise 的失败语义表达：翻译成 取到(失败(消息))，
# 可被 尝试/捕获 接住，与 Python 的 raise + except 逐字对拍。


def boom():
    raise RuntimeError('炸了')


def main():
    try:
        boom()
    except Exception:
        print('接住')
    try:
        raise ValueError('故意的')
    except Exception as e:
        # 注意：AGL 的捕获变量打印出来是完整的中文错误描述，
        # 与 Python 打印异常消息不同 —— 这里只验证「能接住」。
        print('捕获了')


if __name__ == '__main__':
    main()
