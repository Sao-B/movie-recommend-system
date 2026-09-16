from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from sqlalchemy import and_, or_
from ..db import db
from ..models import MovieComment, Movie, UserRating

comments_bp = Blueprint("comments", __name__)


def _own_comment(user_id: int, movie_id: int):
    """该用户在这部电影下的影评。一人一片一条，所以最多命中一条。"""
    return (
        MovieComment.query
        .filter_by(user_id=user_id, movie_id=movie_id)
        .order_by(MovieComment.created_at.desc())
        .first()
    )


def _star_rating(user_id: int, movie_id: int):
    """该用户此刻对这部电影的有效评分；没评过返回 ``None``。"""
    rating = (
        db.session.query(UserRating.rating)
        .filter_by(user_id=user_id, movie_id=movie_id)
        .scalar()
    )
    return float(rating) if rating is not None else None


def _serialize(comment: MovieComment, user_rating):
    """影评 + 作者星级 + 可见性。

    ``visible`` 直接由「作者此刻有没有评分」推导，而不是在影评表里存一个状态位：
    作者在个人中心删掉自己的评分时 ``user_ratings`` 那一行被删除，影评就自动退化为
    「仅自己可见」；用户恢复评分（>= 0.5）后同一行影评又自动重新公开。
    单一事实来源，不会出现两份状态互相打架。
    """
    data = comment.to_dict()
    data["user_rating"] = user_rating
    data["visible"] = user_rating is not None
    return data


@comments_bp.post("/movies/<int:movie_id>/comments")
@jwt_required()
def create_comment(movie_id: int):
    """POST /api/movies/<id>/comments — 发表/更新影评

    Body: ``{ "content": "..." }``，``content`` 必填。

    详情页把「提交评分」与「发表影评」合成一个按钮，且两者**缺一不可**：按钮在
    「有评分但没正文」或「有正文但没评分」时都点不动（见 CommentSection 的
    ``canSubmit``）。这里同样把关——没有给这部电影打分（``user_ratings`` 里没有
    那一行）就不算「成对发表」，直接 400，免得绕过前端留下一条没有评分的影评。
    前端因此必须**先 POST ``/api/ratings/``、再调本接口**，顺序不能反。

    同一用户对同一电影只保留一条影评（再次提交即更新，不追加），与
    ``user_ratings`` 的「一人一片一条」保持一致，避免反复点按钮堆出重复评论。
    走「再次发表」这条路同样算修改正文，会记 ``edited_at``。

    例外只有一处：作者先在影评卡片上选「保留影评草稿」删掉了自己的评分，那条影评
    退成「仅自己可见」（可见性在 ``list_comments`` 里推导）——此时改正文要走
    ``PUT /api/comments/<id>``，本接口仍会拒掉（要重新成对发表，得先把评分补上）。
    """
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()

    if not content:
        return jsonify({"msg": "评论内容不能为空"}), 400

    # 检查电影是否存在
    movie = db.session.get(Movie, movie_id)
    if not movie:
        return jsonify({"msg": "电影不存在"}), 404

    if _star_rating(user_id, movie_id) is None:
        return jsonify({"msg": "请先给这部电影评分，评分和影评需要一起发表"}), 400

    comment = _own_comment(user_id, movie_id)

    # 已有影评则更新，没有则新建
    if comment is not None:
        comment.content = content
        comment.edited_at = datetime.now(timezone.utc)
        status_code = 200
    else:
        comment = MovieComment(
            user_id=user_id,
            movie_id=movie_id,
            content=content
        )
        db.session.add(comment)
        status_code = 201

    db.session.commit()

    return jsonify(_serialize(comment, _star_rating(user_id, movie_id))), status_code


@comments_bp.get("/movies/<int:movie_id>/comments")
@jwt_required(optional=True)
def list_comments(movie_id: int):
    """GET /api/movies/<id>/comments?page=1&per_page=10 — 影评列表（附带作者星级）

    可见性规则：一条影评是否公开展示，取决于**作者此刻有没有给这部电影打分**
    （``user_ratings`` 里有没有那一行）。作者在个人中心删掉评分、或在影评卡片的
    确认框里选了「保留影评」（只删评分）之后，他的影评随之退化为「仅自己可见」；
    恢复评分（>= 0.5）后自动重新公开——所以删评分不需要改动影评表。发表侧不允许
    产生这种状态（没有评分发不出影评，见 ``create_comment``），所以它只会由
    「删除 → 保留影评」这一条路径产生。

    时间线按发表时间倒序，只含公开影评；但当前登录用户自己的影评一定可见
    （哪怕处于隐藏状态）。此外用 ``my_comment`` 单独返回一份当前用户的影评，
    供详情页固定在**原评论输入框的位置**置顶展示（已发表时那个输入框不再显示）；
    ``my_comment.visible`` 为 ``false`` 时前端会标注「仅自己可见」。

    ``jwt_required(optional=True)``：游客也能看评论列表，不会 401；未登录时
    ``my_comment`` 为 ``null``，且隐藏影评一律不返回。
    """
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(50, request.args.get("per_page", 10, type=int))

    identity = get_jwt_identity()
    viewer_id = int(identity) if identity is not None else None

    # 公开条件 = JOIN 命中「作者有评分」；本人的影评不受此限
    visible = UserRating.id.isnot(None)
    if viewer_id is not None:
        visible = or_(visible, MovieComment.user_id == viewer_id)

    # 一次性查出影评与作者评分（LEFT JOIN，未评分时星级为 NULL）
    query = (
        db.session.query(MovieComment, UserRating.rating)
        .outerjoin(
            UserRating,
            and_(
                UserRating.user_id == MovieComment.user_id,
                UserRating.movie_id == movie_id
            )
        )
        .filter(MovieComment.movie_id == movie_id, visible)
        .order_by(MovieComment.created_at.desc())
    )

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    comments = [
        _serialize(comment, float(rating) if rating is not None else None)
        for comment, rating in pagination.items
    ]

    my_comment = None
    if viewer_id is not None:
        mine = _own_comment(viewer_id, movie_id)
        if mine is not None:
            my_comment = _serialize(mine, _star_rating(viewer_id, movie_id))

    return jsonify({
        "comments": comments,
        "my_comment": my_comment,
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages,
    })


@comments_bp.put("/comments/<int:comment_id>")
@jwt_required()
def update_comment(comment_id: int):
    """PUT /api/comments/<id> — 编辑自己的影评

    Body: ``{ "content": "..." }``，正文必填——把正文改成空不算「编辑」，
    想撤下影评请走 ``DELETE /api/comments/<id>``。

    只允许编辑自己的影评（他人的返回 403，不存在返回 404）。编辑成功写入
    ``edited_at``，详情页据此显示「上次修改于 …」；从未编辑过则为 ``null``。
    """
    user_id = int(get_jwt_identity())
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()

    comment = db.session.get(MovieComment, comment_id)
    if comment is None:
        return jsonify({"msg": "评论不存在"}), 404
    if comment.user_id != user_id:
        return jsonify({"msg": "无权编辑他人评论"}), 403
    if not content:
        return jsonify({"msg": "评论内容不能为空"}), 400

    comment.content = content
    comment.edited_at = datetime.now(timezone.utc)
    db.session.commit()

    return jsonify(_serialize(comment, _star_rating(user_id, comment.movie_id))), 200


@comments_bp.delete("/comments/<int:comment_id>")
@jwt_required()
def delete_comment(comment_id: int):
    """DELETE /api/comments/<id>[?with_rating=1] — 删除自己的影评（可连评分一起删）

    影评与评分在库里仍是两行独立的数据（``movie_comments`` / ``user_ratings``），
    所以默认只删影评这一行；带 ``with_rating=1`` 时连同当前用户对该电影的评分一起删。

    详情页影评卡片上的「删除」（置顶那条与时间线里自己那条是同一个组件）弹出的确认框
    会问「影评草稿要不要保留」，两个选项分别落到两个接口：
    - 保留草稿 → ``DELETE /api/ratings/<movie_id>``，只删评分，影评留着但退成
      「仅自己可见」（可见性由 ``list_comments`` 推导，恢复评分即自动重新公开）；
    - 不保留 → 走本接口 + ``with_rating=1``，两行在**同一次 commit** 里删掉，
      不会出现「影评没了、评分还留着」的半截状态；用户本来就没评分时是空操作，不报错。

    只允许删自己的影评（他人的 403，不存在的 404），权限判断在删任何一行之前完成。
    """
    user_id = int(get_jwt_identity())
    with_rating = request.args.get("with_rating", "").lower() in ("1", "true", "yes")

    comment = db.session.get(MovieComment, comment_id)
    if not comment:
        return jsonify({"msg": "评论不存在"}), 404

    if comment.user_id != user_id:
        return jsonify({"msg": "无权删除他人评论"}), 403

    # 先记下电影：删完 comment 之后属性还在，但读 movie_id 更清楚
    movie_id = comment.movie_id
    db.session.delete(comment)

    rating_deleted = False
    if with_rating:
        rating = UserRating.query.filter_by(
            user_id=user_id, movie_id=movie_id
        ).first()
        if rating is not None:
            db.session.delete(rating)
            rating_deleted = True

    db.session.commit()

    return jsonify({"msg": "删除成功", "with_rating": rating_deleted}), 200
