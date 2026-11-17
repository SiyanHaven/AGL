# 期望输出: 与原 Python 一致
# 特性: pow 三参（模幂）+ from-import math/random 放行
from math import ceil, floor
from random import randint, choice, seed


def main() -> None:
    seed(1)
    print(ceil(2.1), floor(2.9))
    print(pow(7, 3, 10))
    print(randint(1, 100), choice([1, 2, 3]))


if __name__ == "__main__":
    main()
