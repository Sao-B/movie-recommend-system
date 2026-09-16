from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, jwt_required
from sqlalchemy import func
from ..db import db
from ..models import Movie, MovieRatingStats, UserRating
from ..rating_stats import count_ratings, merge_counts, summarize_counts

movies_bp = Blueprint("movies", __name__)

# 常量定义
_PAGE_SIZE = 20
MIN_RATING_COUNT_FOR_POPULAR = 100  # 热门电影的最小评分人数阈值（入榜门票）
M_PRIOR = 500  # 贝叶斯先验门槛：热门池评分人数的中位数（实测 524，取整）


@movies_bp.get("/")
def list_movies():
    """GET /api/movies?page=1&per_page=20&genre=Action&q=keyword&year=1995&sort=popular"""
    page     = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", _PAGE_SIZE, type=int))
    genre    = request.args.get("genre", "").strip()
    keyword  = request.args.get("q", "").strip()
    year     = request.args.get("year", type=int)
    sort     = request.args.get("sort", "popular").strip()  # popular, latest, movie_id

    query = Movie.query

    if genre:
        query = query.filter(Movie.genres.like(f"%{genre}%"))
    if keyword:
        query = query.filter(Movie.title.ilike(f"%{keyword}%"))
    if year:
        query = query.filter(Movie.year == year)

    # 排序逻辑：使用 movies 表中的 MovieLens 统计数据
    if sort == "popular":
        # 首页热门榜：IMDB 式贝叶斯加权评分，避免"小样本高分"霸榜：
        #   WR = v/(v+m) * R + m/(v+m) * C   （R=均分, v=人数, m=先验门槛, C=全库均分）
        # m 取热门池（rating_count>=100）评分人数的中位数 ≈ 500（实测 524），
        # 即"典型上榜电影"的话语权恰好一半；不做海报过滤，排序完全由公式决定。
        global_mean = (
            db.session.query(func.avg(Movie.avg_rating))
            .filter(Movie.avg_rating.isnot(None))
            .scalar()
        ) or 0.0
        m = M_PRIOR
        weighted = (
            func.coalesce(Movie.rating_count, 0)
            / (func.coalesce(Movie.rating_count, 0) + m)
            * func.coalesce(Movie.avg_rating, 0)
            + m / (func.coalesce(Movie.rating_count, 0) + m)
            * global_mean
        )
        query = query.filter(Movie.rating_count >= MIN_RATING_COUNT_FOR_POPULAR)
        query = query.order_by(weighted.desc(), func.coalesce(Movie.rating_count, 0).desc())
    elif sort == "latest":
        query = query.order_by(func.coalesce(Movie.year, 0).desc())
    else:
        query = query.order_by(Movie.movie_id)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    movies = [movie.to_dict() for movie in pagination.items]

    return jsonify({
        "movies":   movies,
        "total":    pagination.total,
        "page":     page,
        "per_page": per_page,
        "pages":    pagination.pages,
    })


@movies_bp.get("/<int:movie_id>")
def get_movie(movie_id: int):
    """GET /api/movies/<id> — 电影详情 + MovieLens 平均评分"""
    movie = db.session.get(Movie, movie_id)
    if movie is None:
        return jsonify({"msg": "电影不存在"}), 404

    # 直接返回 Movie 模型中的 MovieLens 统计数据
    return jsonify(movie.to_dict())


@movies_bp.get("/<int:movie_id>/rating-stats")
@jwt_required(optional=True)
def get_movie_rating_stats(movie_id: int):
    """GET /api/movies/<id>/rating-stats — 该电影的评分统计（公开）

    统计口径 = **MovieLens 全量评分 + 本站用户评分合并**，页面上不区分来源：
    平均分按评分条数加权，五档分布逐档相加。

    - MovieLens 侧取 ``movie_rating_stats``（``scripts/build_ml_rating_stats.py``
      离线聚合好的五档分布与精确总和），没有该片数据时视为 0；
    - 本站侧取 ``user_ratings``，逐条用 ``rating_to_star_bucket`` 归档，与离线
      聚合共用同一份档位规则。

    无需登录即可查看；携带有效 JWT 时额外返回当前用户对这部电影的评分
    （``my_rating``，未评过或未登录为 ``null``），供详情页回显星级。

    ``jwt_required(optional=True)`` 是关键：没有 Authorization 头也放行，
    只有带了 token 才解析身份，不会因为游客访问而返回 401。
    """
    if db.session.get(Movie, movie_id) is None:
        return jsonify({"msg": "电影不存在"}), 404

    local_ratings = [
        row[0]
        for row in db.session.query(UserRating.rating)
        .filter(UserRating.movie_id == movie_id)
        .all()
    ]

    ml = db.session.get(MovieRatingStats, movie_id)
    if ml is not None:
        counts = merge_counts(ml.star_counts(), count_ratings(local_ratings))
        total = (ml.rating_count or 0) + len(local_ratings)
        rating_sum = float(ml.rating_sum or 0) + sum(local_ratings)
    else:
        counts = count_ratings(local_ratings)
        total = len(local_ratings)
        rating_sum = sum(local_ratings)

    stats = summarize_counts(counts, total, rating_sum)

    my_rating = None
    identity = get_jwt_identity()
    if identity is not None:
        mine = UserRating.query.filter_by(
            user_id=int(identity), movie_id=movie_id
        ).first()
        my_rating = mine.rating if mine else None

    return jsonify({
        "movie_id":     movie_id,
        "count":        stats["count"],
        "average":      stats["average"],
        "distribution": stats["distribution"],
        "my_rating":    my_rating,
    })


@movies_bp.get("/genres")
def list_genres():
    """GET /api/movies/genres — 返回所有类型列表"""
    rows = db.session.query(Movie.genres).filter(Movie.genres.isnot(None)).all()
    genres = set()
    for (g,) in rows:
        genres.update(g.split("|"))
    genres.discard("(no genres listed)")
    return jsonify(sorted(genres))
