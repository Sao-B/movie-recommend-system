import os
from datetime import timedelta

import yaml

# 项目根目录（backend/ 的上一级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_cfg = _load()
_mysql = _cfg["mysql"]
_flask = _cfg["flask"]
_als = _cfg["als"]


class Config:
    # Flask / JWT
    SECRET_KEY = _flask["secret_key"]
    JWT_SECRET_KEY = _flask["secret_key"]
    # access token 有效期（小时），默认 168h = 7 天；config.yaml 的 jwt 段可覆盖
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(
        hours=int(_cfg.get("jwt", {}).get("access_token_expires_hours", 168))
    )

    # SQLAlchemy
    SQLALCHEMY_DATABASE_URI = (
        f"mysql+pymysql://{_mysql['user']}:{_mysql['password']}"
        f"@{_mysql['host']}:{_mysql['port']}"
        f"/{_mysql['database']}?charset={_mysql['charset']}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # CORS
    CORS_ORIGINS = _cfg["cors"]["origins"]

    # 服务器
    FLASK_HOST = _flask["host"]
    FLASK_PORT = int(_flask["port"])
    FLASK_DEBUG = bool(_flask["debug"])

    # 用户头像上传目录（config.yaml 的 flask.avatar_upload_dir 可覆盖；
    # 留空时路由按 backend/uploads/avatars 兜底）
    AVATAR_UPLOAD_DIR = _flask.get("avatar_upload_dir")

    # ALS / online recommendation
    ALS_TOP_N = int(_als.get("top_n", 20))
    ALS_REG_PARAM = float(_als.get("reg_param", 0.1))
    ALS_NONNEGATIVE = True
    # 候选集门槛已移除（2026-10-01）：原先的 ALS_MIN_RATING_COUNT
    # （`als.min_rating_count`，可辨识性门槛 n_i >= rank）整体删除，
    # 在线排序现在对全量因子进行。详见 recommendation_service._candidate_movie_ids。

    # ItemCF 在线推荐（config.yaml 的 itemcf 段；model_dir 为空 = 不启用该链路）
    # 相对路径按项目根（BASE_DIR）解析——与 flask.avatar_upload_dir 的约定一致，
    # 不受进程工作目录影响。
    _itemcf = _cfg.get("itemcf", {})
    _itemcf_dir = _itemcf.get("model_dir") or None
    if _itemcf_dir and not os.path.isabs(_itemcf_dir):
        _itemcf_dir = os.path.join(BASE_DIR, _itemcf_dir)
    ITEMCF_MODEL_DIR = _itemcf_dir
    ITEMCF_TOP_N = int(_itemcf.get("top_n", _als.get("top_n", 20)))
    # V4 三路融合推荐（config.yaml 的 fusion 段；enabled=false = 不启用）
    _fusion = _cfg.get("fusion", {})
    FUSION_ENABLED = bool(_fusion.get("enabled", False))
    FUSION_WEIGHTS = tuple(float(w) for w in _fusion.get(
        "weights", [0.4, 0.5, 0.1]))
    FUSION_ALS_MIN_SUPPORT = int(_fusion.get("als_min_support", 1000))
    FUSION_ALS_TOP_K = int(_fusion.get("als_top_k", 500))
    FUSION_TOP_N = int(_fusion.get("top_n", _als.get("top_n", 20)))
