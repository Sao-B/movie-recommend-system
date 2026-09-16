"""用户个人主页相关路由"""
import os
import re
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, desc
from app.db import db
from app.models import UserRating, Movie, MovieComment, User
from app.pagination import get_pagination

profile_bp = Blueprint('profile', __name__, url_prefix='/api/profile')

# ---------- 头像文件 ----------
# 头像是本地上传的图片，users.avatar_url 里存的是本服务的读取地址（相对路径，
# 换域名/端口都不用改库；以 http 开头的老数据仍然按外链直接由浏览器加载）。
AVATAR_URL_PREFIX = "/api/profile/avatar/"
ALLOWED_AVATAR_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_AVATAR_BYTES = 2 * 1024 * 1024
# 只放行本模块自己生成的文件名，读取路由据此挡住 ../ 之类的路径穿越
_AVATAR_FILENAME_RE = re.compile(r"^avatar_(\d+)_[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp)$")
# 魔数校验：前端的 accept 属性可以绕过，按文件头判断比只看扩展名稳
_IMAGE_MAGIC = {
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".webp": (b"RIFF",),
}


def avatar_upload_dir() -> str:
    """头像存放目录。

    config.yaml 的 ``flask.avatar_upload_dir`` 可覆盖，默认 ``backend/uploads/avatars``。
    返回绝对路径——send_from_directory 与 os.remove 都按它定位文件。
    """
    configured = current_app.config.get("AVATAR_UPLOAD_DIR")
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(
        os.path.join(current_app.root_path, os.pardir, "uploads", "avatars")
    )


def _looks_like_image(data: bytes, ext: str) -> bool:
    return any(data.startswith(sig) for sig in _IMAGE_MAGIC[ext])


def _delete_local_avatar(avatar_url, upload_dir: str) -> None:
    """换头像时删掉上一张本地上传的图；外链头像（http 开头）不碰。"""
    if not avatar_url or not avatar_url.startswith(AVATAR_URL_PREFIX):
        return
    filename = avatar_url[len(AVATAR_URL_PREFIX):]
    if not _AVATAR_FILENAME_RE.match(filename):
        return
    try:
        os.remove(os.path.join(upload_dir, filename))
    except OSError:
        pass  # 文件早就不在了，不影响新头像落库


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
    """更新用户信息（用户名、头像、签名）

    头像是「点自己的头像上传」那条链路写的，正常不从这里改；
    仍保留 avatar_url 字段是为了兼容把头像设成外链的旧数据 / 手工调用。
    """
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


@profile_bp.post("/avatar")
@jwt_required()
def upload_avatar():
    """上传当前用户的头像（multipart/form-data，字段名 file）。

    校验顺序：扩展名 → 文件头 → 大小。旧图在新图落盘后删除，
    所以中途失败不会把用户已有的头像弄丢。
    """
    user_id = int(get_jwt_identity())
    user = db.session.get(User, user_id)

    if not user:
        return jsonify({"error": "用户不存在"}), 404

    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "请选择要上传的图片"}), 400

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_AVATAR_EXTS:
        return jsonify({"error": "只支持 jpg / png / gif / webp 格式的图片"}), 400

    # 多读一个字节就能判断是否超限，不必把超大文件整个读进内存
    data = file.read(MAX_AVATAR_BYTES + 1)
    if len(data) > MAX_AVATAR_BYTES:
        return jsonify({"error": "图片不能超过 2MB"}), 400
    if not data:
        return jsonify({"error": "图片内容为空"}), 400
    if not _looks_like_image(data, ext):
        return jsonify({"error": "文件内容不是有效图片"}), 400

    upload_dir = avatar_upload_dir()
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"avatar_{user_id}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(upload_dir, filename), "wb") as fh:
        fh.write(data)

    _delete_local_avatar(user.avatar_url, upload_dir)
    # 同一个用户每次上传都是新文件名，浏览器的图片缓存不会串到旧图上
    user.avatar_url = AVATAR_URL_PREFIX + filename
    db.session.commit()

    return jsonify(user.to_dict())


@profile_bp.get("/avatar/<filename>")
def get_avatar(filename):
    """读取头像图片本身。

    不加鉴权：头像是公开信息（后续评论区、影评卡都会直接 <img> 引它），
    文件名本身带 32 位随机串，猜不到也就拿不到。
    """
    if not _AVATAR_FILENAME_RE.match(filename):
        return jsonify({"error": "头像不存在"}), 404

    return send_from_directory(avatar_upload_dir(), filename)


@profile_bp.get("/stats")
@jwt_required()
def get_user_stats():
    """获取用户观影统计数据"""
    user_id = int(get_jwt_identity())

    # 基础统计
    total_ratings = db.session.query(func.count(UserRating.id)).filter(
        UserRating.user_id == user_id
    ).scalar() or 0

    avg_rating = db.session.query(func.avg(UserRating.rating)).filter(
        UserRating.user_id == user_id
    ).scalar()
    avg_rating = round(float(avg_rating), 2) if avg_rating else 0

    total_comments = db.session.query(func.count(MovieComment.id)).filter(
        MovieComment.user_id == user_id
    ).scalar() or 0

    # 评分分布（0-5星，步长0.5）
    rating_distribution = {}
    for i in range(11):  # 0, 0.5, 1.0, ..., 5.0
        rating_value = i * 0.5
        count = db.session.query(func.count(UserRating.id)).filter(
            UserRating.user_id == user_id,
            UserRating.rating == rating_value
        ).scalar() or 0
        rating_distribution[str(rating_value)] = count

    # 最喜欢的类型 Top 3
    favorite_genres = db.session.query(
        Movie.genres,
        func.count(UserRating.id).label('count')
    ).join(
        UserRating, UserRating.movie_id == Movie.movie_id
    ).filter(
        UserRating.user_id == user_id,
        UserRating.rating >= 4.0,  # 只统计高分电影
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
        func.count(UserRating.id).label('count')
    ).join(
        UserRating, UserRating.movie_id == Movie.movie_id
    ).filter(
        UserRating.user_id == user_id,
        UserRating.rating >= 4.0,
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


@profile_bp.get("/comments")
@jwt_required()
def get_user_comments():
    """获取用户评论列表"""
    user_id = int(get_jwt_identity())

    # 分页参数
    page, page_size = get_pagination(param="page_size")

    # 构建查询
    # 评论列表里要带上该用户对这部电影的评分（评分可已被单独删掉，所以是外连接）
    query = db.session.query(
        MovieComment, Movie, UserRating.rating
    ).join(
        Movie, Movie.movie_id == MovieComment.movie_id
    ).outerjoin(
        UserRating,
        (UserRating.user_id == MovieComment.user_id)
        & (UserRating.movie_id == MovieComment.movie_id)
    ).filter(
        MovieComment.user_id == user_id
    ).order_by(
        desc(MovieComment.created_at)
    )

    # 分页
    total = query.count()
    results = query.limit(page_size).offset((page - 1) * page_size).all()

    items = []
    for comment, movie, rating in results:
        items.append({
            "comment_id": comment.id,
            "movie_id": movie.movie_id,
            "title": movie.title,
            "year": movie.year,
            "poster_url": movie.poster_url,
            "content": comment.content,
            "user_rating": float(rating) if rating is not None else None,
            "created_at": comment.created_at.isoformat() if comment.created_at else None
        })

    return jsonify({
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    })
