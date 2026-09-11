from flask import Blueprint, request, jsonify
from sqlalchemy import func
from ..db import db
from ..models import Movie, Rating

movies_bp = Blueprint("movies", __name__)

_PAGE_SIZE = 20


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
        # 按平均评分排序（主要），评分人数为次要
        # 过滤掉评分人数少于 100 的电影，避免小样本偏差
        query = query.filter(Movie.rating_count >= 100)
        query = query.order_by(
            func.coalesce(Movie.avg_rating, 0).desc(),
            func.coalesce(Movie.rating_count, 0).desc()
        )
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


@movies_bp.get("/genres")
def list_genres():
    """GET /api/movies/genres — 返回所有类型列表"""
    rows = db.session.query(Movie.genres).filter(Movie.genres.isnot(None)).all()
    genres = set()
    for (g,) in rows:
        genres.update(g.split("|"))
    genres.discard("(no genres listed)")
    return jsonify(sorted(genres))
