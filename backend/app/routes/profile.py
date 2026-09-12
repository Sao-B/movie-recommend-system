"""用户个人主页相关路由"""
from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, desc
from app.db import db
from app.models import Rating, Movie, MovieComment, User

profile_bp = Blueprint('profile', __name__, url_prefix='/api/profile')


@profile_bp.get("/info")
@jwt_required()
def get_user_info():
    """获取用户基本信息"""
    user_id = int(get_jwt_identity())
    user = db.session.get(User, user_id)

    if not user:
        return jsonify({"error": "用户不存在"}), 404

    return jsonify(user.to_dict())


@profile_bp.put("/info")
@jwt_required()
def update_user_info():
    """更新用户信息（用户名、头像、签名）"""
    user_id = int(get_jwt_identity())
    user = db.session.get(User, user_id)

    if not user:
        return jsonify({"error": "用户不存在"}), 404

    data = request.get_json()

    # 更新用户名
    if "username" in data:
        new_username = data["username"].strip()
        if not new_username:
            return jsonify({"error": "用户名不能为空"}), 400
        if len(new_username) > 50:
            return jsonify({"error": "用户名不能超过50个字符"}), 400

        # 检查用户名是否已被占用
        existing = db.session.query(User).filter(
            User.username == new_username,
            User.id != user_id
        ).first()
        if existing:
            return jsonify({"error": "用户名已被占用"}), 400

        user.username = new_username

    # 更新头像 URL
    if "avatar_url" in data:
        avatar_url = data["avatar_url"]
        if avatar_url and len(avatar_url) > 500:
            return jsonify({"error": "头像 URL 过长"}), 400
        user.avatar_url = avatar_url if avatar_url else None

    # 更新个人签名
    if "bio" in data:
        bio = data["bio"]
        if bio and len(bio) > 500:
            return jsonify({"error": "个人签名不能超过500个字符"}), 400
        user.bio = bio if bio else None

    db.session.commit()

    return jsonify(user.to_dict())



@profile_bp.get("/stats")
@jwt_required()
def get_user_stats():
    """获取用户观影统计数据"""
    user_id = int(get_jwt_identity())

    # 基础统计
    total_ratings = db.session.query(func.count(Rating.id)).filter(
        Rating.user_id == user_id
    ).scalar() or 0

    avg_rating = db.session.query(func.avg(Rating.rating)).filter(
        Rating.user_id == user_id
    ).scalar()
    avg_rating = round(float(avg_rating), 2) if avg_rating else 0

    total_comments = db.session.query(func.count(MovieComment.id)).filter(
        MovieComment.user_id == user_id
    ).scalar() or 0

    # 评分分布（0-5星，步长0.5）
    rating_distribution = {}
    for i in range(11):  # 0, 0.5, 1.0, ..., 5.0
        rating_value = i * 0.5
        count = db.session.query(func.count(Rating.id)).filter(
            Rating.user_id == user_id,
            Rating.rating == rating_value
        ).scalar() or 0
        rating_distribution[str(rating_value)] = count

    # 最喜欢的类型 Top 3
    favorite_genres = db.session.query(
        Movie.genres,
        func.count(Rating.id).label('count')
    ).join(
        Rating, Rating.movie_id == Movie.movie_id
    ).filter(
        Rating.user_id == user_id,
        Rating.rating >= 4.0,  # 只统计高分电影
        Movie.genres.isnot(None)
    ).group_by(
        Movie.genres
    ).order_by(
        desc('count')
    ).limit(3).all()

    # 解析类型（genres 可能是 "Action|Drama|Sci-Fi" 格式）
    genre_counts = {}
    for genres_str, count in favorite_genres:
        if genres_str:
            for genre in genres_str.split('|'):
                genre = genre.strip()
                if genre:
                    genre_counts[genre] = genre_counts.get(genre, 0) + count

    top_genres = sorted(genre_counts.items(), key=lambda x: x[1], reverse=True)[:3]

    # 最喜欢的导演 Top 3
    favorite_directors = db.session.query(
        Movie.director,
        func.count(Rating.id).label('count')
    ).join(
        Rating, Rating.movie_id == Movie.movie_id
    ).filter(
        Rating.user_id == user_id,
        Rating.rating >= 4.0,
        Movie.director.isnot(None),
        Movie.director != ''
    ).group_by(
        Movie.director
    ).order_by(
        desc('count')
    ).limit(3).all()

    return jsonify({
        "total_ratings": total_ratings,
        "avg_rating": avg_rating,
        "total_comments": total_comments,
        "rating_distribution": rating_distribution,
        "top_genres": [{"genre": g, "count": c} for g, c in top_genres],
        "top_directors": [{"director": d, "count": c} for d, c in favorite_directors]
    })


@profile_bp.get("/ratings")
@jwt_required()
def get_user_ratings():
    """获取用户评分列表"""
    user_id = int(get_jwt_identity())

    # 分页参数
    from flask import request
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)
    sort = request.args.get('sort', 'time_desc')  # time_desc, time_asc, rating_desc, rating_asc

    # 构建查询
    query = db.session.query(
        Rating, Movie
    ).join(
        Movie, Movie.movie_id == Rating.movie_id
    ).filter(
        Rating.user_id == user_id
    )

    # 排序
    if sort == 'time_desc':
        query = query.order_by(desc(Rating.rated_at))
    elif sort == 'time_asc':
        query = query.order_by(Rating.rated_at)
    elif sort == 'rating_desc':
        query = query.order_by(desc(Rating.rating))
    elif sort == 'rating_asc':
        query = query.order_by(Rating.rating)

    # 分页
    total = query.count()
    results = query.limit(page_size).offset((page - 1) * page_size).all()

    items = []
    for rating, movie in results:
        items.append({
            "movie_id": movie.movie_id,
            "title": movie.title,
            "year": movie.year,
            "genres": movie.genres,
            "poster_url": movie.poster_url,
            "director": movie.director,
            "rating": rating.rating,
            "rated_at": rating.rated_at.isoformat() if rating.rated_at else None
        })

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    })


@profile_bp.get("/comments")
@jwt_required()
def get_user_comments():
    """获取用户评论列表"""
    user_id = int(get_jwt_identity())

    # 分页参数
    from flask import request
    page = request.args.get('page', 1, type=int)
    page_size = request.args.get('page_size', 20, type=int)

    # 构建查询
    query = db.session.query(
        MovieComment, Movie
    ).join(
        Movie, Movie.movie_id == MovieComment.movie_id
    ).filter(
        MovieComment.user_id == user_id
    ).order_by(
        desc(MovieComment.created_at)
    )

    # 分页
    total = query.count()
    results = query.limit(page_size).offset((page - 1) * page_size).all()

    items = []
    for comment, movie in results:
        items.append({
            "comment_id": comment.id,
            "movie_id": movie.movie_id,
            "title": movie.title,
            "year": movie.year,
            "poster_url": movie.poster_url,
            "content": comment.content,
            "created_at": comment.created_at.isoformat() if comment.created_at else None
        })

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    })
