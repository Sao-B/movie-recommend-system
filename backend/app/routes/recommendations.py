from flask import Blueprint, current_app, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity

rec_bp = Blueprint("recommendations", __name__)


@rec_bp.get("/")
@jwt_required()
def get_recommendations():
    """GET /api/recommendations — 返回当前用户的个性化推荐列表（JWT 必须）"""
    user_id = int(get_jwt_identity())
    # 服务在 create_app() 中初始化，此处直接取用，不做懒加载
    service = current_app.extensions["recommendation_service"]

    recs = service.get_for_user(user_id)

    if not recs:
        return jsonify({
            "msg":  "暂无推荐数据，请先评分或等待模型首次发布",
            "data": [],
        })

    return jsonify({
        "user_id": user_id,
        "count":   len(recs),
        "data":    recs,
    })
