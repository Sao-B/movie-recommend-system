"""电影搜索框的匹配规则（纯函数，前台 / 后台两个列表接口共用）。

一个搜索框吃四种输入：

| 输入 | 命中方式 |
|------|----------|
| ``ID318`` / ``id318`` | **只**按 ``movie_id = 318`` 精确命中，结果就那一条 |
| ``318``   | 片名 / 简介 含 "318" 的片子 **OR** ``movie_id = 318``，ID 那条排最前 |
| ``toy``   | 片名子串命中 ``Toy Story`` |
| ``监狱``  | 简介子串命中 ``overview`` |

也就是说：**想精确找某部片的 ID 就用 ``id`` / ``ID`` 前缀**，写法只有两种形式——
``ID318`` 或 ``id318``（前缀大小写随便，但**数字必须紧挨着前缀**：`id 318`、`ID=318`、
`id:318` 都不算，会被当成普通关键词去搜片名 / 简介）。这种写法不掺任何片名 / 简介匹配；
不带前缀的纯数字仍然走「宽匹配」，方便顺手搜到片名里带数字的片子。

性能说明：这里用 ``LIKE`` 而不是 SQLAlchemy 的 ``ilike()``。``movies.title`` /
``overview`` 的排序规则是 ``utf8mb4_0900_ai_ci``，``LIKE`` 本来就大小写不敏感
（实测 ``'%toy story%'`` 与 ``'%TOY STORY%'`` 都命中 9 条），而 ``ilike()`` 会把
两边都套上 ``LOWER()``，在 8.7 万行的 ``overview``（TEXT）上白烧一遍函数调用——
实测单次扫描 583ms → 383ms。**换成别的库 / 换成区分大小写的排序规则时这条要重新确认。**
"""

import re

from sqlalchemy import or_

from .models import Movie

#: MySQL INT 上限。超出这个范围的纯数字关键词不可能命中任何 movie_id，
#: 拿去和 INT 列比较会触发 "Out of range value" 警告，因此提前挡掉。
_MAX_MOVIE_ID = 2 ** 31 - 1

#: 显式 ID 只有两种写法：``ID318`` / ``id318``——前缀大小写随意，但**数字必须紧挨着**。
#: 中间加空格、等号、冒号（``id 318`` / ``ID=318``）都不算，会退回普通关键词匹配。
_EXPLICIT_ID_RE = re.compile(r"^id(\d+)$", re.IGNORECASE)


def parse_movie_id(keyword: str):
    """关键词整体就是一个十进制整数时返回 int，否则返回 None。

    用于「不带 id 前缀的纯数字」那条宽匹配分支，以及调用方的 ID 优先排序。
    用 ``str.isdecimal()`` 而不是 ``isdigit()``：上标数字（``²``）也满足
    ``isdigit()``，但 ``int("²")`` 会抛 ValueError。
    """
    text = (keyword or "").strip()
    if not text.isdecimal():
        return None
    value = int(text)
    return value if 0 <= value <= _MAX_MOVIE_ID else None


def parse_explicit_id(keyword: str):
    """``id318`` 这类显式 ID 输入返回 int，其它返回 None。"""
    match = _EXPLICIT_ID_RE.match((keyword or "").strip())
    if match is None:
        return None
    value = int(match.group(1))
    return value if 0 <= value <= _MAX_MOVIE_ID else None


def movie_keyword_filter(keyword: str):
    """关键词 → SQLAlchemy 过滤条件。

    ``idN`` 只认 ``movie_id``；其它关键词是「片名 OR 简介（OR 纯数字时的 movie_id）」。
    """
    text = (keyword or "").strip()

    explicit_id = parse_explicit_id(text)
    if explicit_id is not None:
        return Movie.movie_id == explicit_id

    pattern = f"%{text}%"
    conditions = [Movie.title.like(pattern), Movie.overview.like(pattern)]
    movie_id = parse_movie_id(text)
    if movie_id is not None:
        conditions.append(Movie.movie_id == movie_id)
    return or_(*conditions)
