# 期望输出: 与原 Python 一致
def main() -> None:
    try:
        print(1)
    except Exception as e:
        print("出错了")
    finally:
        print("结束")
    try:
        print(1 / 0)
    except Exception as e:
        print("出错了")
    finally:
        print("结束")


if __name__ == "__main__":
    main()
