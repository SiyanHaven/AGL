# 期望输出: 与原 Python 一致
def sum_all(*nums):
    return sum(nums)


def main():
    print(sum_all(1, 2, 3))
    print(sum_all(4, 5, 6, 7))


if __name__ == "__main__":
    main()
