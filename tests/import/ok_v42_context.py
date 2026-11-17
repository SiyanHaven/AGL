# v0.42：自定义上下文管理器 with + __enter__/__exit__
# 手动展开成 try/finally：enter() → body → exit()，即使 body 抛错也走 exit。

class MyResource:
    def __init__(self, name):
        self.name = name
        self.opened = False
    def __enter__(self):
        self.opened = True
        return self
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.opened = False
        return False

def main():
    r = MyResource("文件A")
    print(r.opened)
    with r as ctx:
        print(ctx.opened)
        print(ctx.name)
    print(r.opened)


if __name__ == '__main__':
    main()
