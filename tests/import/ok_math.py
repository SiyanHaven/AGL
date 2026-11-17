# 期望输出: 与原 Python 一致
def add(a: int, b: int) -> int:
    return a + b


def grade(score: int) -> str:
    if score >= 90:
        return "优"
    elif score >= 60:
        return "良"
    else:
        return "差"


def main() -> None:
    total = add(60, 50)
    print(grade(total))
    print(add(1, 2), add(10, 20))


if __name__ == "__main__":
    main()
