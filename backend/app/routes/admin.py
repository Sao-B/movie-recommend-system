from functools import wraps
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import func
from ..db import db
from ..account_rules import username_error
from ..models import User, Movie, UserRating, ModelVersion, Recommendation, MovieComment
from ..movie_search import movie_keyword_filter
from ..pagination import get_pagination

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


def apply_sort(query, columns, default_sort, default_order):
    """按表头点击传来的 sort/order 排序；白名单外的字段一律忽略，防止任意列注入。"""
    sort = request.args.get("sort", default_sort)
    order = request.args.get("order", default_order)
    column = columns.get(sort, columns[default_sort])
    return query.order_by(column.desc() if order == "desc" else column.asc())


# ==================== 统计数据 API ====================

@admin_bp.get("/stats")
@admin_required
def get_stats():
    """获取系统统计数据"""
    user_count = db.session.query(func.count(User.id)).scalar() or 0
    movie_count = db.session.query(func.count(Movie.movie_id)).scalar() or 0
    rating_count = db.session.query(func.count(UserRating.id)).scalar() or 0
    recommendation_count = db.session.query(func.count(Recommendation.id)).scalar() or 0

    active_model = ModelVersion.query.filter_by(status='active').first()

    # 最近10条评分
    recent_ratings_query = db.session.query(
        UserRating.id,
        UserRating.rating,
        User.username,
        Movie.title.label('movie_title')
    ).join(User, UserRating.user_id == User.id)\
     .join(Movie, UserRating.movie_id == Movie.movie_id)\
     .order_by(UserRating.rated_at.desc())\
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
    page, per_page = get_pagination()
    q = request.args.get("q", "").strip()

    query = User.query
    if q:
        query = query.filter(User.username.ilike(f"%{q}%"))

    query = apply_sort(
        query,
        {"id": User.id, "username": User.username, "created_at": User.created_at},
        "created_at",
        "desc",
    )

    pagination = query.paginate(
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
    """创建新用户

    账号走与注册同一套命名规则（``app/account_rules.py``），昵称初始与账号同名。
    """
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role = data.get("role", "user")

    if not username or not password:
        return jsonify({"msg": "账号和密码不能为空"}), 400
    invalid = username_error(username)
    if invalid:
        return jsonify({"msg": invalid}), 400
    if role not in ["user", "admin"]:
        return jsonify({"msg": "角色必须是 user 或 admin"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"msg": "账号已存在"}), 409

    user = User(username=username, nickname=username, role=role)
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
            # 后台改账号算特权操作，但字符规则与注册保持一致（app/account_rules.py）
            invalid = username_error(new_username)
            if invalid:
                return jsonify({"msg": invalid}), 400
            if User.query.filter_by(username=new_username).first():
                return jsonify({"msg": "账号已存在"}), 409
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

    # recommendations 没有指向 users 的外键（只按 ID 关联，见 schema.sql），必须手动清理；
    # user_ratings / movie_comments 的外键是 ON DELETE CASCADE，本可自动级联，
    # 这里仍显式删一遍——把删除范围写在代码里，不依赖库的外键配置。
    deleted = {}
    deleted["ratings"] = UserRating.query.filter_by(user_id=user_id).delete()
    deleted["comments"] = MovieComment.query.filter_by(user_id=user_id).delete()
    deleted["recommendations"] = Recommendation.query.filter_by(user_id=user_id).delete()

    db.session.delete(user)
    db.session.commit()
    return jsonify({"msg": "删除成功", "deleted": deleted})


# ==================== 电影管理 API ====================

@admin_bp.get("/movies")
@admin_required
def list_movies():
    """获取电影列表（分页）

    ``q`` 与前台 ``GET /api/movies`` 同一套规则（见 ``app/movie_search.py``）：
    ``ID318``（小写 ``id318`` 同样认）只按 ID 精确命中，``318`` 宽匹配片名 / 简介（ID 命中排最前），
    文字则在片名与简介里做子串匹配。后台按 ID 查片不用再去翻列表。
    """
    page, per_page = get_pagination()
    q = request.args.get("q", "").strip()
    genres = request.args.get("genres", "").strip()
    year = request.args.get("year", type=int)

    query = Movie.query
    if q:
        query = query.filter(movie_keyword_filter(q))
    if genres:
        query = query.filter(Movie.genres.like(f"%{genres}%"))
    if year:
        query = query.filter(Movie.year == year)

    query = apply_sort(
        query,
        {
            "movie_id": Movie.movie_id,
            "title": Movie.title,
            "genres": Movie.genres,
            "year": Movie.year,
            "avg_rating": Movie.avg_rating,
            "rating_count": Movie.rating_count,
        },
        "movie_id",
        "asc",
    )

    pagination = query.paginate(
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

    # 同 delete_user：user_ratings / recommendations 没有指向 movies 的外键，必须手动清理；
    # movie_comments 与 movie_rating_stats 是 ON DELETE CASCADE，这里仍显式删一遍。
    deleted = {}
    deleted["ratings"] = UserRating.query.filter_by(movie_id=movie_id).delete()
    deleted["comments"] = MovieComment.query.filter_by(movie_id=movie_id).delete()
    deleted["recommendations"] = Recommendation.query.filter_by(movie_id=movie_id).delete()

    db.session.delete(movie)
    db.session.commit()
    return jsonify({"msg": "删除成功", "deleted": deleted})


# ==================== 评分管理 API ====================

@admin_bp.get("/ratings")
@admin_required
def list_ratings():
    """获取评分列表（分页）"""
    page, per_page = get_pagination()
    user_id = request.args.get("user_id", type=int)
    movie_id = request.args.get("movie_id", type=int)

    # 使用 join 避免 N+1 查询
    query = db.session.query(
        UserRating,
        User.username,
        Movie.title
    ).join(User, UserRating.user_id == User.id)\
     .join(Movie, UserRating.movie_id == Movie.movie_id)

    if user_id:
        query = query.filter(UserRating.user_id == user_id)
    if movie_id:
        query = query.filter(UserRating.movie_id == movie_id)

    query = apply_sort(
        query,
        {
            "id": UserRating.id,
            "user_id": UserRating.user_id,
            "movie_id": UserRating.movie_id,
            "username": User.username,
            "movie_title": Movie.title,
            "rating": UserRating.rating,
            "rated_at": UserRating.rated_at,
        },
        "rated_at",
        "desc",
    )

    pagination = query.paginate(
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
    rating = db.session.get(UserRating, rating_id)
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
