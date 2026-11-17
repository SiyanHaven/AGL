# 无类型注解参数取 len(x)：导入器翻成 x.长度，
# 检查器必须放行「任意」类型取 .长度（运行期才见分晓）。


def size(x):
    return len(x)


def main():
    print(size([1, 2, 3]))
    print(size('abc'))


if __name__ == '__main__':
    main()
