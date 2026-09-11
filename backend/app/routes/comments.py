from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from ..db import db
from ..models import MovieComment, Movie

comments_bp = Blueprint("comments", __name__)


@comments_bp.post("/movies/<int:movie_id>/comments")
@jwt_required()
def create_comment(movie_id: int):
    """POST /api/movies/<id>/comments — 发表评论"""
    user_id = int(get_jwt_identity())
    data = request.get_json()

    if not data or not data.get("content", "").strip():
        return jsonify({"msg": "评论内容不能为空"}), 400

    content = data["content"].strip()

    # 检查电影是否存在
    movie = db.session.get(Movie, movie_id)
    if not movie:
        return jsonify({"msg": "电影不存在"}), 404

    # 创建评论
    comment = MovieComment(
        user_id=user_id,
        movie_id=movie_id,
        content=content
    )
    db.session.add(comment)
    db.session.commit()

    return jsonify(comment.to_dict()), 201


@comments_bp.get("/movies/<int:movie_id>/comments")
def list_comments(movie_id: int):
    """GET /api/movies/<id>/comments?page=1&per_page=10 — 获取评论列表（附带用户评分）"""
    from ..models import Rating
    from sqlalchemy import and_

    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(50, request.args.get("per_page", 10, type=int))

    # 使用 LEFT JOIN 一次性查出评论和对应的用户评分
    query = (
        db.session.query(MovieComment, Rating.rating)
        .outerjoin(
            Rating,
            and_(
                Rating.user_id == MovieComment.user_id,
                Rating.movie_id == movie_id
            )
        )
        .filter(MovieComment.movie_id == movie_id)
        .order_by(MovieComment.created_at.desc())
    )

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    # 组装评论数据，附带评分
    comments = []
    for comment, user_rating in pagination.items:
        comment_data = comment.to_dict()
        comment_data['user_rating'] = user_rating
        comments.append(comment_data)

    return jsonify({
        "comments": comments,
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages,
    })


@comments_bp.delete("/comments/<int:comment_id>")
@jwt_required()
def delete_comment(comment_id: int):
    """DELETE /api/comments/<id> — 删除自己的评论"""
    user_id = int(get_jwt_identity())

    comment = db.session.get(MovieComment, comment_id)
    if not comment:
        return jsonify({"msg": "评论不存在"}), 404

    if comment.user_id != user_id:
        return jsonify({"msg": "无权删除他人评论"}), 403

    db.session.delete(comment)
    db.session.commit()

    return jsonify({"msg": "删除成功"}), 200
