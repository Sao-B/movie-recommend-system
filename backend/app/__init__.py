from flask import Flask, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from .db import db
from .routes.auth import auth_bp
from .routes.movies import movies_bp
from .routes.recommendations import rec_bp
from .routes.ratings import ratings_bp


def create_app(config_obj=None):
    app = Flask(__name__)

    # 加载配置
    if config_obj is None:
        import sys, os
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from config import Config
        config_obj = Config

    app.config.from_object(config_obj)

    # 扩展初始化
    db.init_app(app)
    # CORS_ORIGINS 未配置时默认限制本地开发端口，不开放 "*"
    origins = app.config.get("CORS_ORIGINS") or ["http://localhost:5173"]
    CORS(app, origins=origins)
    JWTManager(app)

    # 蓝图注册
    app.register_blueprint(auth_bp,    url_prefix="/api/auth")
    app.register_blueprint(movies_bp,  url_prefix="/api/movies")
    app.register_blueprint(rec_bp,     url_prefix="/api/recommendations")
    app.register_blueprint(ratings_bp, url_prefix="/api/ratings")

    # 全局错误处理
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"msg": "资源不存在"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"msg": "服务器内部错误"}), 500

    return app
