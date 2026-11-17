# for/else 直译：range 展开式 当 循环和 遍历 循环都支持 否则，
# break 打断后 else 不执行，与 Python 逐字对拍。


def main():
    for i in range(2):
        print(i)
    else:
        print('for-else')
    xs = [1, 2, 3]
    for y in xs:
        if y == 2:
            break
    else:
        print('for-no-break')
    print('done')


if __name__ == '__main__':
    main()