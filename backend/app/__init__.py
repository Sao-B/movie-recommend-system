from flask import Flask, jsonify
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from sqlalchemy import text
from .db import db
from .routes.auth import auth_bp
from .routes.movies import movies_bp
from .routes.recommendations import rec_bp
from .routes.ratings import ratings_bp
from .routes.admin import admin_bp
from .routes.comments import comments_bp
from .routes.profile import profile_bp
from .routes.dislikes import dislikes_bp


def create_app(config_obj=None):
    app = Flask(__name__)

    # 加载配置
    if config_obj is None:
        from config import Config
        config_obj = Config

    app.config.from_object(config_obj)

    # 扩展初始化
    db.init_app(app)
    # CORS_ORIGINS 未配置时默认限制本地开发端口，不开放 "*"
    origins = app.config.get("CORS_ORIGINS") or ["http://localhost:5173"]
    CORS(app, origins=origins)
    jwt = JWTManager(app)

    # 坏 token（格式错误/签名不符）默认返回 422，前端的 401 拦截器接不住，
    # 用户会卡在「带坏 token 请求」的状态里无法回到登录页。统一按 401 处理。
    @jwt.invalid_token_loader
    def _invalid_token(_reason):
        return jsonify({"msg": "登录凭证无效，请重新登录"}), 401

    # 在线推荐服务：应用级单例，避免多线程环境下重复构建因子缓存
    from .recommendation_service import OnlineRecommendationService
    from .recommendation_repository import SqlAlchemyRecommendationRepository
    app.extensions["recommendation_service"] = OnlineRecommendationService(
        SqlAlchemyRecommendationRepository(),
        top_n=app.config.get("ALS_TOP_N", 20),
    )

    # ItemCF 在线推荐（可选）：配置了 itemcf.model_dir 才启用 ?model=itemcf 链路。
    # 模型图在首次请求时 mmap 加载（约 127MB），不拖慢应用启动。
    if app.config.get("ITEMCF_MODEL_DIR"):
        from .itemcf_service import ItemCFService
        app.extensions["itemcf_service"] = ItemCFService(
            SqlAlchemyRecommendationRepository(),
            app.config["ITEMCF_MODEL_DIR"],
            top_n=app.config.get("ITEMCF_TOP_N", 20),
        )
    if app.config.get("FUSION_ENABLED") and app.config.get("ITEMCF_MODEL_DIR"):
        from .fusion_service import FusionService
        app.extensions["fusion_service"] = FusionService(
            app.extensions["itemcf_service"],
            app.extensions["itemcf_service"].repository,
            weights=app.config.get("FUSION_WEIGHTS", (0.4, 0.5, 0.1)),
            als_min_support=app.config.get("FUSION_ALS_MIN_SUPPORT", 1000),
            als_top_k=app.config.get("FUSION_ALS_TOP_K", 500),
            top_n=app.config.get("FUSION_TOP_N", 20),
        )

    # 蓝图注册
    app.register_blueprint(auth_bp,    url_prefix="/api/auth")
    app.register_blueprint(movies_bp,  url_prefix="/api/movies")
    app.register_blueprint(rec_bp,     url_prefix="/api/recommendations")
    app.register_blueprint(ratings_bp, url_prefix="/api/ratings")
    app.register_blueprint(admin_bp,   url_prefix="/api/admin")
    app.register_blueprint(comments_bp, url_prefix="/api")
    app.register_blueprint(profile_bp, url_prefix="/api/profile")
    app.register_blueprint(dislikes_bp, url_prefix="/api/dislikes")

    # 健康探针（`GET /api/health`，不鉴权）
    #
    # 给启动脚本用：`scripts/start_bg.ps1` 必须等它返回 200 才打开浏览器。
    # 前端（vite）两秒就绪，后端冷启动（重启机器后第一次）实测要 ~20s 才 bind 端口，
    # 早先「只等前端端口」的做法会让首页那次 /api/movies/ 直撞代理的 ECONNREFUSED，
    # 页面停在空列表上、只能手动刷新。
    # 顺带做一次 SELECT 1：进程活着但库连不上时返回 503，前端也就没必要白等。
    @app.get("/api/health")
    def health():
        try:
            db.session.execute(text("SELECT 1"))
        except Exception:  # noqa: BLE001 —— 探针只关心通不通，异常类型不重要
            return jsonify({"status": "degraded", "database": "down"}), 503
        return jsonify({"status": "ok", "database": "up"})

    # 全局错误处理
    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"msg": "资源不存在"}), 404

    @app.errorhandler(500)
    def server_error(e):
        return jsonify({"msg": "服务器内部错误"}), 500

    return app
