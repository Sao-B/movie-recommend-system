"""电影评分统计的纯计算逻辑。

与数据访问层解耦：这里只接收「评分数字」或「档位计数」，不碰数据库、不碰 Flask，
方便单元测试。数据访问在 ``app/routes/movies.py`` 的 rating-stats 接口里。

档位规则只有这里一份实现——SQL 侧不重复写 ROUND()，离线聚合脚本
``scripts/build_ml_rating_stats.py`` 也 import 这里的 ``rating_to_star_bucket``，
保证「MovieLens 分布」与「本站分布」用的是同一个口径，不会出现 4.5 在一处算
5 星、在另一处算 4 星。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, Mapping, Optional

#: 返回顺序固定为 5 星在前，前端直接按顺序渲染即可
STAR_LEVELS = (5, 4, 3, 2, 1)

#: 允许的评分区间（与 routes/ratings.py 的校验保持一致）
MIN_RATING = 0.5
MAX_RATING = 5.0


def empty_counts() -> Dict[int, int]:
    """五档计数器的零值：``{5: 0, 4: 0, 3: 0, 2: 0, 1: 0}``。"""
    return {star: 0 for star in STAR_LEVELS}


def rating_to_star_bucket(rating: float) -> int:
    """把一条评分归入 1~5 的整数档位。

    前端 ``el-rate`` 允许半星（0.5 步长），归档规则为四舍五入：
    4.5 归 5 档、4.4 归 4 档、0.5 归 1 档。与 ``el-rate`` 显示时的取整
    语义一致，避免「星星显示 5 颗、分布里算 4 星」这种自相矛盾。

    越界值按边界档位收敛，不抛异常——脏数据不应该让整个统计接口挂掉。
    """
    bucket = int(float(rating) + 0.5)
    return min(STAR_LEVELS[0], max(STAR_LEVELS[-1], bucket))


def count_ratings(ratings: Iterable[float]) -> Dict[int, int]:
    """把一串具体评分归入五档，返回 ``{star: count}``。"""
    counts = empty_counts()
    for value in ratings:
        counts[rating_to_star_bucket(value)] += 1
    return counts


def merge_counts(*count_maps: Optional[Mapping[int, int]]) -> Dict[int, int]:
    """把多组五档计数逐档相加。

    用于合并「MovieLens 离线聚合的分布」与「本站用户评分的分布」——
    合并后页面上就是一个整体，不区分来源。
    """
    merged = empty_counts()
    for counts in count_maps:
        if not counts:
            continue
        for star in STAR_LEVELS:
            merged[star] += int(counts.get(star, 0) or 0)
    return merged


def summarize_counts(
    counts: Mapping[int, int],
    total: Optional[int] = None,
    rating_sum: Optional[float] = None,
) -> Dict[str, Any]:
    """把五档计数汇总成总条数、平均分与五档分布。

    :param counts: ``{star: count}``，缺的档位按 0 处理
    :param total: 总条数；缺省时取各档之和
    :param rating_sum: 评分总和。**给了就用它算精确平均分**；不给则退化为
        用档位值 1..5 加权求和（例如分布来自「只有分档计数、没有原始明细」
        的场景），这是近似值——能拿到精确总和时不要走这条路

    返回结构::

        {
            "count": 27,
            "average": 4.04,          # 无评分时为 None
            "distribution": [          # 固定 5 项，5 星 → 1 星
                {"stars": 5, "count": 12, "percent": 44.4},
                ...
            ],
        }
    """
    counts = {star: int(counts.get(star, 0) or 0) for star in STAR_LEVELS}
    total = sum(counts.values()) if total is None else int(total)

    if total <= 0:
        average = None
    elif rating_sum is None:
        average = round(sum(star * counts[star] for star in STAR_LEVELS) / total, 2)
    else:
        average = round(float(rating_sum) / total, 2)

    distribution = [
        {
            "stars": star,
            "count": counts[star],
            "percent": round(counts[star] * 100 / total, 1) if total else 0.0,
        }
        for star in STAR_LEVELS
    ]

    return {"count": total, "average": average, "distribution": distribution}
