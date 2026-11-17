# finally：自身 return 覆盖 try 里的 return；异常沿 finally 冒泡；
# try 内 return 后 finally 仍执行
def f1() -> int:
    try:
        return 1
    finally:
        return 2


def f2() -> int:
    try:
        return 1
    finally:
        print("fin")


def main() -> None:
    print(f1())          # finally 里的 return 覆盖 → 2
    print(f2())          # finally 打印后，原 return 1 生效


if __name__ == "__main__":
    main()
