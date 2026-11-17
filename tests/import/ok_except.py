# 期望输出: 与原 Python 一致
def main() -> None:
    try:
        print(1)
    except ValueError as e:
        print("数字错了")

    try:
        print(2)
    except Exception as e:
        print("出错了")


if __name__ == "__main__":
    main()
