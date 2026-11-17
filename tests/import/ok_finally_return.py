# try 内提前 return + finally：finally 的打印要在 return 之前执行，与 Python 一致
def main() -> int:
    try:
        return 1
    except Exception as e:
        print("出错了")
    finally:
        print("结束")


if __name__ == "__main__":
    main()
