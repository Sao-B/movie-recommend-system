from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.exc import IntegrityError
from ..db import db
from ..models import Rating, Movie

ratings_bp = Blueprint("ratings", __name__)


@ratings_bp.post("/")
@jwt_required()
def submit_rating():
    """POST /api/ratings — 提交或更新评分（JWT 必须）

    Body: { "movie_id": 1, "rating": 4.5 }
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

    # 已有评分则更新，没有则新建
    existing = Rating.query.filter_by(
        user_id=user_id, movie_id=movie_id
    ).first()

    if existing:
        existing.rating = rating
        status_code = 200
    else:
        existing = Rating(
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


@ratings_bp.get("/me")
@jwt_required()
def my_ratings():
    """GET /api/ratings/me — 当前用户的历史评分（JWT 必须）"""
    user_id = int(get_jwt_identity())
    recs = (
        Rating.query
        .filter_by(user_id=user_id)
        .order_by(Rating.rated_at.desc())
        .limit(200)
        .all()
    )
    return jsonify({"count": len(recs), "data": [r.to_dict() for r in recs]})
