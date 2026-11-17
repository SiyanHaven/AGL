# 语言：中文
# 语义：v0.32——表达式位置三元提升（if/else 拆开，惰性一致）· is None 判空直译 ·
#       映射合并 | · 分行列表 splitlines / 三切分 partition
def main():
    x = 15
    print("大" if x > 10 else "小")
    y = 3
    print("偶" if y % 2 == 0 else "奇")
    print(x if y > 5 else y)
    v = None
    if v is None:
        print("空")
    else:
        print("非空")
    w = 7
    if w is not None:
        print("有值")
    d1 = {"a": 1}
    d2 = {"b": 2}
    d3 = d1 | d2
    print(len(d3))
    print(d3["a"])
    print(d3["b"])
    s = "甲\n乙\n丙"
    lines = s.splitlines()
    print(len(lines))
    print(lines[0])
    print(lines[2])
    print("a-b-c".partition("-")[0])
    print("a-b-c".partition("-")[1])
    print("a-b-c".partition("-")[2])
    print("xyz".partition("-")[0])
    print("xyz".partition("-")[1] == "")


if __name__ == "__main__":
    main()
