from flask import Blueprint, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy.orm import joinedload
from ..models import Recommendation

rec_bp = Blueprint("recommendations", __name__)


@rec_bp.get("/")
@jwt_required()
def get_recommendations():
    """GET /api/recommendations — 返回当前用户的个性化推荐列表（JWT 必须）"""
    user_id = int(get_jwt_identity())

    # 使用 joinedload 避免 N+1 查询问题
    recs = (
        Recommendation.query
        .options(joinedload(Recommendation.movie))
        .filter_by(user_id=user_id)
        .order_by(Recommendation.rank)
        .all()
    )

    if not recs:
        return jsonify({
            "msg":  "暂无推荐数据，请先评几部电影或等待下次离线训练",
            "data": [],
        })

    return jsonify({
        "user_id": user_id,
        "count":   len(recs),
        "data":    [r.to_dict() for r in recs],
    })
