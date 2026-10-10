from flask import Blueprint, current_app, jsonify, request
from flask_jwt_extended import jwt_required, get_jwt_identity

rec_bp = Blueprint("recommendations", __name__)


@rec_bp.get("/")
@jwt_required()
def get_recommendations():
    """GET /api/recommendations — 返回当前用户的个性化推荐列表（JWT 必须）

    查询参数 ``model``：
    - ``als``（默认）：ALS 因子在线求解（原链路，不变）；
    - ``itemcf``：ItemCF 冻结邻居图查表打分（需配置 itemcf.model_dir）。
    """
    user_id = int(get_jwt_identity())
    model = request.args.get("model", "als")

    if model == "itemcf":
        service = current_app.extensions.get("itemcf_service")
        if service is None:
            return jsonify({
                "msg": "ItemCF 推荐未启用：请先在 config.yaml 配置 itemcf.model_dir",
                "data": [],
            }), 400
        recs = service.recommendations_for(user_id)
        if not recs:
            # ItemCF 无米下锅（无好评历史/证据为空）→ 共用类型加权热门兜底
            als_service = current_app.extensions.get("recommendation_service")
            if als_service is not None:
                recs = als_service.get_popular_for_user(user_id)
    elif model == "fusion":
        service = current_app.extensions.get("fusion_service")
        if service is None:
            return jsonify({
                "msg": "融合推荐未启用：请检查 config.yaml 的 fusion.enabled "
                       "与 itemcf.model_dir",
                "data": [],
            }), 400
        recs = service.recommendations_for(user_id)
        if not recs:
            als_service = current_app.extensions.get("recommendation_service")
            if als_service is not None:
                recs = als_service.get_popular_for_user(user_id)
    else:
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
        "model":   model,
        "count":   len(recs),
        "data":    recs,
    })
