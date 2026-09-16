"""SQLite 测试支撑：把模型的 BigInteger 主键临时换成 Integer。

SQLite 只有 ``INTEGER PRIMARY KEY`` 才是 rowid 自增别名，模型的 ``BigInteger``
主键直接 ``create_all()`` 会报 ``NOT NULL constraint failed``。此补丁只影响
本进程内的 DDL，不改生产模型。用法见 ``test_comment_flow.py`` / ``test_auth_routes.py``。
"""
import sys
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import BigInteger, Integer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT / "backend") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.models import Movie, MovieComment, User, UserRating  # noqa: E402

PK_MODELS = (Movie, User, UserRating, MovieComment)


@contextmanager
def sqlite_integer_pk_patch():
    """with 期间把 BigInteger 主键列临时换成 Integer，退出时还原。"""
    saved = {}
    for model in PK_MODELS:
        for col in model.__table__.primary_key.columns:
            saved[(model.__tablename__, col.name)] = col.type
            if isinstance(col.type, BigInteger):
                col.type = Integer()
    try:
        yield saved
    finally:
        for model in PK_MODELS:
            for col in model.__table__.primary_key.columns:
                previous = saved.get((model.__tablename__, col.name))
                if previous is not None:
                    col.type = previous
