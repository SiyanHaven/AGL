# **d 动态展开：目标形参有 **kw 时，**d 直译为映射表实参（AGL 的 收集 收任意个映射表）
def collect(**kw):
    return len(kw)


def show(tag, **kw):
    s = tag + ":" + str(len(kw))
    for k in kw:
        s += f";{k}={kw[k]}"
    return s


def main() -> None:
    d = {"a": 1, "b": 2}
    print(collect(**d))
    print(collect(x=1, **d))      # 静态键 + 动态展开 → 合并映射（无冲突）
    print(show("t", x=9, **d))    # 展开 + 静态键混合


if __name__ == "__main__":
    main()
