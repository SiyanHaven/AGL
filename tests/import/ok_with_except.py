# 类型化捕获直译：`except ValueError:` → `捕获 (错: 值错误)`（只捕该类，
# 其它异常继续往上冒，与 Python 一致）；`except Exception` / 裸 except →
# 抓所有（无类型）。逐字对拍。注意：AGL 的 `取到(失败())` 统一抛
# RuntimeError，类型化捕获的宿主异常用真实越界/缺键触发。


def main():
    try:
        x = 10 / 0
        print(x)
    except ZeroDivisionError as e:
        print("除零")
    try:
        z = [1, 2, 3][9]
        print(z)
    except IndexError as e:
        print("越界")
    try:
        m = {"甲": 1}
        print(m["乙"])
    except KeyError as e:
        print("缺键")
    try:
        raise RuntimeError("运行时")
    except Exception:
        print("抓所有")


if __name__ == '__main__':
    main()
