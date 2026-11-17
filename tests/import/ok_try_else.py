# try/except/else 直译成 尝试/捕获/否则：try 没出错才执行 else，
# 与 Python 逐字对拍（含 try 体声明的变量在 else 后仍可见）。


def main():
    try:
        x = 1
    except Exception:
        x = 0
    else:
        print('else-ran')
    print(x)


if __name__ == '__main__':
    main()