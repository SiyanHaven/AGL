# try/except/else/finally 四件套直译：else 在捕获与最后之间，
# 四段行号交错由 emit_try 处理，逐字对拍。

def main():
    try:
        x = 1
    except Exception:
        x = 0
    else:
        print('e')
    finally:
        print('f')
    print(x)


if __name__ == '__main__':
    main()