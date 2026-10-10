"""V5 评分映射与标签权重纯函数（EDA 2026-10-10 定稿参数）。

依据：experiments/itemcf 相邻评分行为分析 + 频率验证（task_freq_validation）。
- 评分映射：负侧斜率加倍（2.5 分的行为学位置靠近 2.0 而远离 3.0）；
- 频率权重：继续观看概率在 freq 5→6 跳变（91.5%→99.2%）后饱和；
- 标签分数：近期同标签均分自校准（高频高差评用户均分 3.06 → 信号 ≈0）。
"""

import math

#: 负侧截断下限（0.5/1.0 分的封顶强度）
W_MIN = -1.2
#: 中性锚点
NEUTRAL = 3.0
#: 频率权重的对数基准（freq=5 时 w=0.5，freq>=50 时 w=1.0）
FREQ_BASE = 5.0


def rating_weight(rating):
    """0.5~5.0 评分 -> [-1.2, 1.0] 反馈权重（分段线性）。

    r < 3 : w = max(r - 3, -1.2)   （0.5/1.0 -> -1.2, 2.0 -> -1.0, 2.5 -> -0.5）
    r >= 3: w = (r - 3) / 2        （3.0 -> 0, 3.5 -> +0.25, 4.0 -> +0.5, 5.0 -> +1.0）
    """
    r = float(rating)
    if r < NEUTRAL:
        return max(r - NEUTRAL, W_MIN)
    return (r - NEUTRAL) / 2.0


def freq_weight(freq):
    """标签累计观看数 -> [0, 1] 召回权重（对数缓和技术饱和段）。

    freq<=0 -> 0；freq=5 -> 0.5；freq=50 -> 1.0。
    """
    f = float(freq)
    if f <= 0:
        return 0.0
    return min(1.0, 0.5 + 0.5 * math.log10(max(f, 1.0) / FREQ_BASE))


def label_signal(recent_mean):
    """近期同标签均分 -> [-1, 1] 排序信号（自校准：3.06 -> ~0, 3.74 -> +0.37）。"""
    s = (float(recent_mean) - NEUTRAL) / 2.0
    return max(-1.0, min(1.0, s))
