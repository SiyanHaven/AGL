# 期望输出: 与原 Python 一致
def main() -> None:
    d = {i: i*2 for i in range(3)}
    print(d[0], d[1], d[2])
    e = {x.upper(): len(x) for x in ["a", "bb", "ccc"]}
    print(e["A"], e["BB"])


if __name__ == "__main__":
    main()
