# 期望输出: 与原 Python 一致
def main() -> None:
    try:
        print(1 / 0)
    except Exception as e:
        print("捕获了")
    names = ["甲"]
    print(names[0] + "好", str(42))


if __name__ == "__main__":
    main()
