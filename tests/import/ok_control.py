# 期望输出: 与原 Python 一致
def total_of(xs: list[int]) -> int:
    total = 0
    for x in xs:
        total = total + x
    return total


def main() -> None:
    items = [3, 1, 2]
    print(items[0], len(items))
    n = 0
    while n < 3:
        n = n + 1
    print(n)
    for i in range(0, 6, 2):
        print(i)
    d = {"甲": 1, "乙": 2}
    print(d["甲"])
    if len(items) > 2 and n == 3:
        print("都满足")
    print(total_of(items))


if __name__ == "__main__":
    main()
