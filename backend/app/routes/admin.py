from functools import wraps
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import func
from ..db import db
from ..models import User, Movie, Rating, ModelVersion, Recommendation

admin_bp = Blueprint('admin', __name__)

def admin_required(fn):
    """管理员权限装饰器：验证当前用户是否为管理员"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()
        user_id = get_jwt_identity()
        user = User.query.get(int(user_id))
        if not user or not user.is_admin():
            return jsonify({"msg": "需要管理员权限"}), 403
        return fn(*args, **kwargs)
    return wrapper


# ==================== 统计数据 API ====================

@admin_bp.get("/stats")
@admin_required
def get_stats():
    """获取系统统计数据"""
    user_count = db.session.query(func.count(User.id)).scalar() or 0
    movie_count = db.session.query(func.count(Movie.movie_id)).scalar() or 0
    rating_count = db.session.query(func.count(Rating.id)).scalar() or 0
    recommendation_count = db.session.query(func.count(Recommendation.id)).scalar() or 0

    active_model = ModelVersion.query.filter_by(status='active').first()

    # 最近10条评分
    recent_ratings_query = db.session.query(
        Rating.id,
        Rating.rating,
        User.username,
        Movie.title.label('movie_title')
    ).join(User, Rating.user_id == User.id)\
     .join(Movie, Rating.movie_id == Movie.movie_id)\
     .order_by(Rating.rated_at.desc())\
     .limit(10).all()

    recent_ratings = [
        {
            "id": r.id,
            "rating": float(r.rating),
            "username": r.username,
            "movie_title": r.movie_title
        }
        for r in recent_ratings_query
    ]

    # 热门电影 Top 10（按评分数量排序）
    top_movies_query = db.session.query(
        Movie.movie_id,
        Movie.title,
        Movie.rating_count
    ).filter(Movie.rating_count > 0)\
     .order_by(Movie.rating_count.desc())\
     .limit(10).all()

    top_movies = [
        {
            "movie_id": m.movie_id,
            "title": m.title,
            "rating_count": m.rating_count
        }
        for m in top_movies_query
    ]

    return jsonify({
        "user_count": user_count,
        "movie_count": movie_count,
        "rating_count": rating_count,
        "recommendation_count": recommendation_count,
        "active_model_version": active_model.version if active_model else None,
        "active_model": {
            "version": active_model.version,
            "rank": active_model.rank,
            "reg_param": active_model.reg_param,
            "activated_at": active_model.activated_at.isoformat() if active_model.activated_at else None
        } if active_model else None,
        "recent_ratings": recent_ratings,
        "top_movies": top_movies
    })


# ==================== 用户管理 API ====================

@admin_bp.get("/users")
@admin_required
def list_users():
    """获取用户列表（分页）"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 20, type=int))
    q = request.args.get("q", "").strip()

    query = User.query
    if q:
        query = query.filter(User.username.ilike(f"%{q}%"))

    pagination = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify({
        "users": [u.to_dict() for u in pagination.items],
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })


@admin_bp.post("/users")
@admin_required
def create_user():
    """创建新用户"""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role = data.get("role", "user")

    if not username or not password:
        return jsonify({"msg": "用户名和密码不能为空"}), 400
    if role not in ["user", "admin"]:
        return jsonify({"msg": "角色必须是 user 或 admin"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"msg": "用户名已存在"}), 409

    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    return jsonify(user.to_dict()), 201


@admin_bp.patch("/users/<int:user_id>")
@admin_required
def update_user(user_id):
    """更新用户信息"""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"msg": "用户不存在"}), 404

    data = request.get_json(silent=True) or {}

    if "username" in data:
        new_username = data["username"].strip()
        if new_username and new_username != user.username:
            if User.query.filter_by(username=new_username).first():
                return jsonify({"msg": "用户名已存在"}), 409
            user.username = new_username

    if "role" in data:
        if data["role"] not in ["user", "admin"]:
            return jsonify({"msg": "角色必须是 user 或 admin"}), 400
        user.role = data["role"]

    if "password" in data and data["password"]:
        user.set_password(data["password"])

    db.session.commit()
    return jsonify(user.to_dict())


@admin_bp.delete("/users/<int:user_id>")
@admin_required
def delete_user(user_id):
    """删除用户"""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"msg": "用户不存在"}), 404

    current_user_id = int(get_jwt_identity())
    if user_id == current_user_id:
        return jsonify({"msg": "不能删除当前登录的管理员"}), 400

    db.session.delete(user)
    db.session.commit()
    return jsonify({"msg": "删除成功"})


# ==================== 电影管理 API ====================

@admin_bp.get("/movies")
@admin_required
def list_movies():
    """获取电影列表（分页）"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 20, type=int))
    q = request.args.get("q", "").strip()

    query = Movie.query
    if q:
        query = query.filter(Movie.title.ilike(f"%{q}%"))

    pagination = query.order_by(Movie.movie_id).paginate(
        page=page, per_page=per_page, error_out=False
    )

    return jsonify({
        "movies": [m.to_dict() for m in pagination.items],
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })


@admin_bp.patch("/movies/<int:movie_id>")
@admin_required
def update_movie(movie_id):
    """更新电影信息"""
    movie = db.session.get(Movie, movie_id)
    if not movie:
        return jsonify({"msg": "电影不存在"}), 404

    data = request.get_json(silent=True) or {}

    if "title" in data and data["title"]:
        movie.title = data["title"].strip()

    if "genres" in data:
        if isinstance(data["genres"], list):
            movie.genres = "|".join(data["genres"])
        elif isinstance(data["genres"], str):
            movie.genres = data["genres"]

    if "year" in data:
        movie.year = data["year"]

    db.session.commit()
    return jsonify(movie.to_dict())


@admin_bp.delete("/movies/<int:movie_id>")
@admin_required
def delete_movie(movie_id):
    """删除电影"""
    movie = db.session.get(Movie, movie_id)
    if not movie:
        return jsonify({"msg": "电影不存在"}), 404

    db.session.delete(movie)
    db.session.commit()
    return jsonify({"msg": "删除成功"})


# ==================== 评分管理 API ====================

@admin_bp.get("/ratings")
@admin_required
def list_ratings():
    """获取评分列表（分页）"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 20, type=int))
    user_id = request.args.get("user_id", type=int)
    movie_id = request.args.get("movie_id", type=int)

    # 使用 join 避免 N+1 查询
    query = db.session.query(
        Rating,
        User.username,
        Movie.title
    ).join(User, Rating.user_id == User.id)\
     .join(Movie, Rating.movie_id == Movie.movie_id)

    if user_id:
        query = query.filter(Rating.user_id == user_id)
    if movie_id:
        query = query.filter(Rating.movie_id == movie_id)

    pagination = query.order_by(Rating.rated_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )

    items = []
    for rating, username, movie_title in pagination.items:
        item = rating.to_dict()
        item["username"] = username
        item["movie_title"] = movie_title
        items.append(item)

    return jsonify({
        "ratings": items,
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })


@admin_bp.delete("/ratings/<int:rating_id>")
@admin_required
def delete_rating(rating_id):
    """删除评分"""
    rating = db.session.get(Rating, rating_id)
    if not rating:
        return jsonify({"msg": "评分不存在"}), 404

    db.session.delete(rating)
    db.session.commit()
    return jsonify({"msg": "删除成功"})


# ==================== 模型管理 API ====================

@admin_bp.get("/models")
@admin_required
def list_models():
    """获取模型列表"""
    models = ModelVersion.query.order_by(ModelVersion.generated_at.desc()).all()

    return jsonify({
        "models": [{
            "version": m.version,
            "rank": m.rank,
            "reg_param": m.reg_param,
            "status": m.status,
            "generated_at": m.generated_at.isoformat() if m.generated_at else None,
            "activated_at": m.activated_at.isoformat() if m.activated_at else None
        } for m in models]
    })


@admin_bp.post("/models/<string:version>/activate")
@admin_required
def activate_model(version):
    """激活指定版本的模型"""
    model = db.session.get(ModelVersion, version)
    if not model:
        return jsonify({"msg": "模型不存在"}), 404

    try:
        # 使用显式事务确保原子性
        ModelVersion.query.filter_by(status='active').update({"status": "retired"})
        model.status = 'active'
        model.activated_at = func.now()
        db.session.commit()

        return jsonify({"msg": "模型已激活", "version": version})
    except Exception as e:
        db.session.rollback()
        return jsonify({"msg": "激活失败"}), 500


@admin_bp.delete("/models/<string:version>")
@admin_required
def delete_model(version):
    """删除指定版本的模型"""
    model = db.session.get(ModelVersion, version)
    if not model:
        return jsonify({"msg": "模型不存在"}), 404

    if model.status == 'active':
        return jsonify({"msg": "不能删除活跃模型，请先激活其他模型"}), 400

    db.session.delete(model)
    db.session.commit()
    return jsonify({"msg": "删除成功"})
