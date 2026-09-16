from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.exc import IntegrityError
from datetime import datetime, timezone
from ..db import db
from ..models import UserRating, Movie

ratings_bp = Blueprint("ratings", __name__)


@ratings_bp.post("/")
@jwt_required()
def submit_rating():
    """POST /api/ratings — 提交或更新评分（JWT 必须）

    Body: { "movie_id": 1, "rating": 4.5 }

    合法取值只有 0.5 ~ 5.0（半星精度），**不接受 0**：详情页撤销评分走的是影评卡片
    「删除 → 保留影评」那条路，它调 ``DELETE /api/ratings/<movie_id>``，不会拿 0 来打
    这个接口。想把某部电影的评分去掉就走那个 DELETE；改分直接再 POST 一次即可（upsert）。

    详情页的「发表 / 修改」要求评分与影评成对提交（先打这个接口、再 POST 影评），
    所以这个接口在前端永远与影评一起被调用；但接口本身仍允许单独提交评分——
    个人中心等场景不受影响。
    """
    user_id = int(get_jwt_identity())
    data    = request.get_json(silent=True) or {}

    movie_id = data.get("movie_id")
    rating   = data.get("rating")

    if movie_id is None or rating is None:
        return jsonify({"msg": "movie_id 和 rating 为必填项"}), 400
    try:
        rating = float(rating)
    except (TypeError, ValueError):
        return jsonify({"msg": "rating 必须是数字"}), 400
    if not (0.5 <= rating <= 5.0):
        return jsonify({"msg": "rating 范围 0.5 ~ 5.0"}), 400
    if not db.session.get(Movie, movie_id):
        return jsonify({"msg": "电影不存在"}), 404

    existing = UserRating.query.filter_by(
        user_id=user_id, movie_id=movie_id
    ).first()

    # 已有评分则更新，没有则新建
    if existing:
        existing.rating = rating
        existing.rated_at = datetime.now(timezone.utc)
        status_code = 200
    else:
        existing = UserRating(
            user_id=user_id,
            movie_id=int(movie_id),
            rating=rating
        )
        db.session.add(existing)
        status_code = 201

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"msg": "评分提交失败，请重试"}), 500

    return jsonify({"msg": "评分成功", "data": existing.to_dict()}), status_code


@ratings_bp.delete("/<int:movie_id>")
@jwt_required()
def delete_rating(movie_id: int):
    """DELETE /api/ratings/<movie_id> — 删除当前用户对该电影的评分（JWT 必须）

    评分是「一人一片一条」，用 movie_id 就能唯一定位当前用户的那条记录，
    不需要评分行 id。删除后该用户的评分统计与详情页回显都会跟着变。

    入口有两处：个人中心「我的评分」列表，以及详情页影评卡片「删除」确认框里的
    「保留影评」（详情页不再为评分单独设删除入口，撤销评分与删影评合并在同一个
    确认框里）。两处都是硬删除，提交后该行从 ``user_ratings`` 消失，不做软删除、
    也不写一条 0 分记录；只删评分时影评仍在，只是退成「仅自己可见」。
    """
    user_id = int(get_jwt_identity())

    rating = UserRating.query.filter_by(
        user_id=user_id, movie_id=movie_id
    ).first()
    if rating is None:
        return jsonify({"msg": "评分不存在"}), 404

    db.session.delete(rating)
    db.session.commit()

    return jsonify({"msg": "评分已删除", "movie_id": movie_id}), 200
