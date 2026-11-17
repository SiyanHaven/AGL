# 期望输出: 与原 Python 一致
# 特性: 异步嵌套函数（await 穿透 / 捕获变量提升 / 三层嵌套 / asyncio.run 入口 / asyncio.sleep）

async def producer():
    base = 100

    async def step1(x):
        async def step2(y):
            return y * 2

        mid = await step2(x)
        return base + mid

    await asyncio.sleep(0.01)
    r = await step1(5)
    print(r)


async def main():
    await producer()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
