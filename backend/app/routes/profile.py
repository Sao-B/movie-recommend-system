"""用户个人主页相关路由"""
import os
import re
import uuid

from flask import Blueprint, current_app, jsonify, request, send_from_directory
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import func, desc
from app.db import db
from app.models import UserRating, Movie, MovieComment, User
from app.genres import CANONICAL_GENRES, MAX_FAVORITE_GENRES
from app.pagination import get_pagination

profile_bp = Blueprint('profile', __name__, url_prefix='/api/profile')

# ---------- 头像文件 ----------
# users.avatar_url 里存的是本服务的读取地址（相对路径，换域名/端口都不用改库）。
# 现在有三种取值：
#   1. 内置 SVG 头像：``/api/profile/avatars/<名字>.svg``（个人主页从清单里挑一个，推荐）
#   2. 历史上传的图片：``/api/profile/avatar/<文件名>``（界面已不再提供上传入口，老数据仍可正常显示）
#   3. 更早的手填外链（http 开头）：由浏览器直接加载，本服务不参与
#   4. NULL：没有头像，前端回落成显示名首字符
AVATAR_URL_PREFIX = "/api/profile/avatar/"
ALLOWED_AVATAR_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
MAX_AVATAR_BYTES = 2 * 1024 * 1024
# 只放行本模块自己生成的文件名，读取路由据此挡住 ../ 之类的路径穿越
_AVATAR_FILENAME_RE = re.compile(r"^avatar_(\d+)_[0-9a-f]{32}\.(jpg|jpeg|png|gif|webp)$")

# ---------- 内置 SVG 头像 ----------
# 清单就是这份常量（顺序即选择面板里的展示顺序），文件在 backend/assets/avatars/ 下同名 .svg。
# 新增头像：把 .svg 放进那个目录，再在这里加一行。
PRESET_AVATAR_URL_PREFIX = "/api/profile/avatars/"
PRESET_AVATARS = (
    ("film", "胶片"),
    ("popcorn", "爆米花"),
    ("clapper", "场记板"),
    ("star", "星星"),
    ("ticket", "电影票"),
    ("projector", "放映机"),
    ("cat", "猫咪"),
    ("robot", "机器人"),
    ("planet", "星球"),
    ("smile", "笑脸"),
)
# 同 _AVATAR_FILENAME_RE：只认「小写字母数字短横 + .svg」，挡住路径穿越
_PRESET_FILENAME_RE = re.compile(r"^[a-z0-9_-]+\.svg$")
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


def preset_avatar_dir() -> str:
    """内置 SVG 头像目录。

    config.yaml 的 ``flask.avatar_preset_dir`` 可覆盖，默认 ``backend/assets/avatars``。
    """
    configured = current_app.config.get("AVATAR_PRESET_DIR")
    if configured:
        return os.path.abspath(configured)
    return os.path.abspath(
        os.path.join(current_app.root_path, os.pardir, "assets", "avatars")
    )


def preset_avatar_url(name: str) -> str:
    """内置头像的名字 → 存进 ``users.avatar_url`` 的地址。"""
    return f"{PRESET_AVATAR_URL_PREFIX}{name}.svg"


def preset_avatar_urls() -> set:
    """全部内置头像地址（PUT 校验用）。"""
    return {preset_avatar_url(name) for name, _ in PRESET_AVATARS}


def _looks_like_image(data: bytes, ext: str) -> bool:
    return any(data.startswith(sig) for sig in _IMAGE_MAGIC[ext])


def _delete_local_avatar(avatar_url, upload_dir: str) -> None:
    """删掉上一张**历史上传**的本地图（外链、内置 SVG 都不是本地文件，不碰）。

    注意 ``/api/profile/avatars/xxx.svg`` 不以 ``AVATAR_URL_PREFIX`` 开头（多的那个 s
    刚好把它挡在外面），所以内置头像不会被误删——改这个常量时别把前缀写成包含关系。
    """
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
    """更新用户信息（昵称、头像、签名）

    **账号（username）不在这里改**：它是登录凭据，注册时按命名规则定下后固定不变，
    请求里带 ``username`` 直接 400。界面上「点一下改」的是**昵称**（``nickname``）——
    昵称任意字符、不唯一，可以改也可以清空（清空后显示回落账号）。

    ``avatar_url`` **只接受内置 SVG 头像的地址，或者 null**（恢复默认）：
    可选值来自 ``GET /api/profile/avatars``，其它地址一律 400——头像不再由前端上传，
    改成从内置清单里挑。换成预设或默认时，会把这位用户历史上传的那张图删掉，不留孤儿文件。
    """
    user_id = int(get_jwt_identity())
    user = db.session.get(User, user_id)

    if not user:
        return jsonify({"error": "用户不存在"}), 404

    data = request.get_json()

    # 账号不可改（老前端会带这个字段过来，明确报错而不是静默忽略）
    if "username" in data:
        return jsonify({"error": "账号不可修改，请改昵称"}), 400

    # 更新昵称：任意字符、不唯一；清空即回落显示账号
    if "nickname" in data:
        nickname = (data["nickname"] or "").strip()
        if len(nickname) > 50:
            return jsonify({"error": "昵称不能超过 50 个字符"}), 400
        user.nickname = nickname or None

    # 更换头像：只认内置清单里的地址，或清空（＝恢复默认，前端回落显示名首字符）
    if "avatar_url" in data:
        avatar_url = data["avatar_url"] or None
        if avatar_url is not None and avatar_url not in preset_avatar_urls():
            return jsonify({"error": "请从内置头像里挑一个，或选择恢复默认"}), 400
        # 换成预设/默认后，这位用户历史上传的那张图不再被引用，顺手删掉
        _delete_local_avatar(user.avatar_url, avatar_upload_dir())
        user.avatar_url = avatar_url

    # 更新个人签名
    if "bio" in data:
        bio = data["bio"]
        if bio and len(bio) > 500:
            return jsonify({"error": "个人签名不能超过500个字符"}), 400
        user.bio = bio if bio else None

    # 更新偏好类型：列表 1..5 项、每项必须是标准类型；空数组 = 明确跳过；
    # 请求里不带这个字段 = 维持原值（首次弹窗与否由 NULL/'[]' 语义区分）。
    if "favorite_genres" in data:
        genres = data["favorite_genres"]
        if not isinstance(genres, list):
            return jsonify({"error": "favorite_genres 必须是数组"}), 400
        if len(genres) > MAX_FAVORITE_GENRES:
            return jsonify({"error": "最多选择 %d 个类型" % MAX_FAVORITE_GENRES}), 400
        invalid = [g for g in genres if g not in CANONICAL_GENRES]
        if invalid:
            return jsonify({"error": "未知类型: %s" % ", ".join(map(str, invalid))}), 400
        user.set_favorite_genres([str(g) for g in genres])

    db.session.commit()

    return jsonify(user.to_dict())


@profile_bp.post("/avatar")
@jwt_required()
def upload_avatar():
    """上传当前用户的头像（multipart/form-data，字段名 file）。

    **界面上已经不再提供这个入口**（个人主页改成从内置 SVG 清单里挑），接口保留是为了：
    历史数据里那批上传过的头像仍由它产生、`tests/test_profile_avatar.py` 仍在覆盖，
    手工调用也还能用。新写的头像不要再走这条。

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
    """读取历史上传的头像图片本身。

    不加鉴权：头像是公开信息（评论区、影评卡都会直接 <img> 引它），
    文件名本身带 32 位随机串，猜不到也就拿不到。
    """
    if not _AVATAR_FILENAME_RE.match(filename):
        return jsonify({"error": "头像不存在"}), 404

    return send_from_directory(avatar_upload_dir(), filename)


@profile_bp.get("/avatars")
def list_preset_avatars():
    """GET /api/profile/avatars — 内置 SVG 头像清单（公开）

    顺序即选择面板里的展示顺序，清单本身是代码里的常量 ``PRESET_AVATARS``，
    不扫目录——这样顺序与名字都是显式的，测试也会逐个断言文件确实在。
    """
    return jsonify([
        {"id": name, "name": label, "url": preset_avatar_url(name)}
        for name, label in PRESET_AVATARS
    ])


@profile_bp.get("/avatars/<filename>")
def get_preset_avatar(filename):
    """GET /api/profile/avatars/<name>.svg — 读取内置 SVG 头像本身（公开）

    就是张静态图，不带鉴权；文件名按 ``_PRESET_FILENAME_RE`` 白名单校验，挡住路径穿越。
    """
    if not _PRESET_FILENAME_RE.match(filename):
        return jsonify({"error": "头像不存在"}), 404

    return send_from_directory(preset_avatar_dir(), filename, mimetype="image/svg+xml")


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

    # 评分分布（0.5~5 星、步长 0.5，共 10 档；0 分不是合法评分，不设 0 档）
    rating_distribution = {}
    for i in range(1, 11):  # 0.5, 1.0, ..., 5.0 —— 评分不含 0 分，不设 0 档
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
