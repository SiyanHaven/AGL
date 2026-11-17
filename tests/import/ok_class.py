# 期望输出: 与原 Python 一致
class Counter:
    def __init__(self, start: int) -> None:
        self.n = start

    def inc(self) -> int:
        self.n += 1
        return self.n


def main() -> None:
    c = Counter(5)
    print(c.inc())
    print(c.inc())
    c.n = 100
    print(c.n)


if __name__ == "__main__":
    main()
