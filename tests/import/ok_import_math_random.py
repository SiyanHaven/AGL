# 期望输出: 与原 Python 一致
# 特性: import math / random 直译（ceil/floor/randint/choice/seed）
import math
import random


def main() -> None:
    random.seed(1)
    print(math.ceil(3.2), math.floor(3.9))
    print(random.randint(1, 100))
    print(random.choice([10, 20, 30]))


if __name__ == "__main__":
    main()
