# 期望输出: 与原 Python 一致
def main() -> None:
    name = "世界"
    age = 30
    print(f"你好，{name}，你今年 {age} 岁")
    print(f"纯字符串")
    print(f"单变量 {age}")

    n = 5
    if 0 < n < 10:
        print("在范围内")

    s = "hello world"
    if "world" in s:
        print("包含 world")
    if "xyz" not in s:
        print("不包含 xyz")

    xs = [1, 2, 3]
    xs[0] = 99
    print(xs[0])

    d = {"甲": 1}
    d["乙"] = 2
    print(d["乙"])

    a = b = 42
    print(a, b)

    xs[1] += 10
    print(xs[1])


if __name__ == "__main__":
    main()
