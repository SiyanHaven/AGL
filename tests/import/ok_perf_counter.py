# 期望输出: 与原 Python 一致
import time


def main():
    t = time.perf_counter()
    print(1 + 1)
    s = time.monotonic()
    print(2 + 3)


if __name__ == "__main__":
    main()
