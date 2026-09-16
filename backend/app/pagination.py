"""Flask 视图共用的分页参数解析。"""
from flask import request


def get_pagination(default_per_page=20, max_per_page=100, param="per_page"):
    """从 query string 解析 page / per_page，越界值收敛到合法区间。

    ``param`` 用于兼容历史上 ``page_size`` 与 ``per_page`` 两种参数名。
    """
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(max_per_page, request.args.get(param, default_per_page, type=int))
    return page, per_page
