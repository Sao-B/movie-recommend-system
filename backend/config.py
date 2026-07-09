import os
import yaml

# 项目根目录（backend/ 的上一级）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load():
    with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


_cfg = _load()
_mysql = _cfg["mysql"]
_flask = _cfg["flask"]


class Config:
    # Flask / JWT
    SECRET_KEY = _flask["secret_key"]
    JWT_SECRET_KEY = _flask["secret_key"]

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
