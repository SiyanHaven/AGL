# 期望输出: 与原 Python 一致
def greet(name: str, prefix: str) -> str:
    return prefix + "，" + name


def sign(n: int) -> str:
    result = "正数" if n >= 0 else "负数"
    return result


def main() -> None:
    print(greet("世界", "你好"))
    print(greet("小明", "早上好"))
    print(sign(5))
    print(sign(-3))
    grade = "及格" if 60 >= 60 else "不及格"
    print(grade)


if __name__ == "__main__":
    main()
