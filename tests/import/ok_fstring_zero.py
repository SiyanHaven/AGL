# f-string 前导零补位：{x:05.2f} 宽 5、两位小数、补零对齐
def main() -> None:
    x = 3.14159
    print(f"{x:05.2f}")


if __name__ == "__main__":
    main()
