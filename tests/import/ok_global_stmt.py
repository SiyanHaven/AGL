# global 声明：模块级只读常量在函数里按「读常量函数」处理
计数 = 0


def main() -> None:
    global 计数
    print(计数)


if __name__ == "__main__":
    main()
