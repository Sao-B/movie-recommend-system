"""账号（``users.username``）的命名规则——注册与后台建号共用一份。

账号是**登录凭据**，也是 ALS 训练里「系统用户」的标识，所以只允许一段干净的
ASCII 字符：字母、数字、下划线、连字符，3–20 位。中文、空格、Emoji、@ 之类
一律不接受。

想用中文之类的名字，那是**昵称**（``users.nickname``）的事，在个人主页随便改，
不受这里约束，也不参与登录。
"""

import re

#: 账号允许的字符与长度
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,20}$")

#: 前端与接口共用的文案（改这里就够，别在别处再抄一遍）
USERNAME_RULE_TEXT = "账号 3-20 位，只能包含字母、数字、下划线、连字符"


def username_error(value: str):
    """账号不合规时返回给用户看的文案，合规返回 ``None``。"""
    if not value:
        return "账号不能为空"
    if not USERNAME_PATTERN.match(value):
        return USERNAME_RULE_TEXT
    return None
