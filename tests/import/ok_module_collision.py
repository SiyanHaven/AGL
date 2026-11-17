# 用户变量与导入模块同名（time）：变量名必须优先，
# 读取侧不能被后端改写成 _agl_time。


import time


def main():
    time = 1
    print(time)


if __name__ == '__main__':
    main()
