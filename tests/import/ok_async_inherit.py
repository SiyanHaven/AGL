# async 祖先方法 + 子类不覆盖：转发函数 B_m 体内调用异步祖先方法 A_m 时
# 必须补「等待」（转发函数自身带「异步」前缀），否则编译报异步函数调用缺等待。
class A:
    async def m(self, x):
        return x + 1

class B(A):
    def other(self):
        return 10

def main():
    b = B()
    print(b.other())

if __name__ == "__main__":
    main()
