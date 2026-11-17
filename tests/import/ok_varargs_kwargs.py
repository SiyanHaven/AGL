# **kwargs：关键字参数打包成映射表参数，调用处打包传入
def collect(**kw):
    return len(kw)


def main():
    print(collect(a=1, b=2))


if __name__ == "__main__":
    main()
