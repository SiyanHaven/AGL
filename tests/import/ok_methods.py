# 期望输出: 与原 Python 一致
def main() -> None:
    s = "  Hello World  "
    print(s.upper())
    print(s.lower())
    print(s.strip())
    print(s.replace("World", "AGL"))
    parts = "a,b,c".split(",")
    print(parts[0], parts[1], parts[2])

    xs = [1, 2]
    xs.append(3)
    xs.append(4)
    print(xs[0], xs[1], xs[2], xs[3])


if __name__ == "__main__":
    main()
