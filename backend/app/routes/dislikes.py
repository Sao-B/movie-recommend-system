"""用户"不感兴趣"标记（推荐硬排除）。

- ``POST /api/dislikes``  ``{"movie_id": 123}``  —— 标记一部电影
- ``DELETE /api/dislikes/<movie_id>``           —— 取消标记
- ``GET /api/dislikes``                          —— 我的全部标记（``movie_ids`` 列表 +
  ``items`` 带片名/年份/类型，个人主页的"偏好设置"卡片直接用它渲染）

语义：标记过的电影在**所有**推荐链路（ItemCF / ALS / 热门兜底）里被硬排除，
与"看过"同级；重复标记幂等（唯一键兜底）。
"""

from flask import Blueprint, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

from app.db import db
from app.models import Movie, UserDislike

dislikes_bp = Blueprint("dislikes", __name__)


@dislikes_bp.post("/")
@jwt_required()
def add_dislike():
    """标记一部电影为不感兴趣（幂等）。"""
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}
    try:
        movie_id = int(data.get("movie_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "movie_id 必须是整数"}), 400

    if not db.session.get(Movie, movie_id):
        return jsonify({"error": "电影不存在"}), 404

    exists = UserDislike.query.filter_by(user_id=user_id, movie_id=movie_id).first()
    if exists:
        return jsonify({"msg": "已标记过", "data": exists.to_dict()})

    dislike = UserDislike(user_id=user_id, movie_id=movie_id)
    db.session.add(dislike)
    db.session.commit()
    return jsonify({"msg": "已标记不感兴趣", "data": dislike.to_dict()}), 201


@dislikes_bp.delete("/<int:movie_id>")
@jwt_required()
def remove_dislike(movie_id: int):
    """取消不感兴趣标记（幂等：不存在也返回成功）。"""
    user_id = int(get_jwt_identity())
    deleted = UserDislike.query.filter_by(user_id=user_id, movie_id=movie_id).delete()
    db.session.commit()
    return jsonify({"msg": "已取消标记" if deleted else "本来就没有标记",
                    "deleted": bool(deleted)})


@dislikes_bp.get("/")
@jwt_required()
def list_dislikes():
    """我的全部不感兴趣标记（movie_id 升序）。

    ``movie_ids`` 保持纯 ID 列表（推荐端只用 ID）；另附 ``items``
    （片名/年份/类型），个人主页的偏好卡片不用再逐个查详情。
    """
    user_id = int(get_jwt_identity())
    rows = (UserDislike.query.filter_by(user_id=user_id)
            .order_by(UserDislike.movie_id).all())
    movie_ids = [r.movie_id for r in rows]

    movies = {}
    if movie_ids:
        found = Movie.query.filter(Movie.movie_id.in_(movie_ids)).all()
        movies = {m.movie_id: m for m in found}
    items = []
    for mid in movie_ids:
        m = movies.get(mid)
        items.append({
            "movie_id": mid,
            # 电影行可能已被后台删除（外键没约束到），此时标题给 None，前端显示占位
            "title": m.title if m else None,
            "year": m.year if m else None,
            "genres": ([g for g in (m.genres or "").split("|") if g] if m else []),
        })
    return jsonify({"count": len(rows), "movie_ids": movie_ids, "items": items})
