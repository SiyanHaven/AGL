# while/else 直译成 当/否则：循环没被 break 打断才执行 else。
# 第二个循环用 break 验证「打断后 else 不执行」。


def main():
    i = 0
    while i < 3:
        i = i + 1
    else:
        print('while-else')
    print(i)
    j = 0
    while j < 5:
        j = j + 1
        if j == 2:
            break
    else:
        print('no-break')
    print(j)


if __name__ == '__main__':
    main()