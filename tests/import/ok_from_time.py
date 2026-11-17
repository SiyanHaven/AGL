# from time import：与 import time 同一条路放行；sleep 翻成 AGL 内建
# （Python 的秒自动换成 AGL 的毫秒），导入语句本身忽略。
from time import sleep


def main():
    sleep(0)
    print("醒了")


if __name__ == "__main__":
    main()
