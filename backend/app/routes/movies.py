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

    # 子查询：计算每部电影的平均评分和评分数
    subquery = (
        db.session.query(
            Rating.movie_id,
            func.avg(Rating.rating).label('avg_rating'),
            func.count(Rating.id).label('rating_count')
        )
        .group_by(Rating.movie_id)
        .subquery()
    )

    # 主查询：关联电影和评分统计
    query = db.session.query(Movie, subquery.c.avg_rating, subquery.c.rating_count)\
        .outerjoin(subquery, Movie.movie_id == subquery.c.movie_id)

    if genre:
        query = query.filter(Movie.genres.like(f"%{genre}%"))
    if keyword:
        query = query.filter(Movie.title.ilike(f"%{keyword}%"))
    if year:
        query = query.filter(Movie.year == year)

    # 排序逻辑
    if sort == "popular":
        # 按评分数和平均评分排序，没有评分的排在后面
        query = query.order_by(
            subquery.c.rating_count.desc().nulls_last(),
            subquery.c.avg_rating.desc().nulls_last()
        )
    elif sort == "latest":
        query = query.order_by(Movie.year.desc().nulls_last())
    else:
        query = query.order_by(Movie.movie_id)

    pagination = query.paginate(page=page, per_page=per_page, error_out=False)

    movies = []
    for movie, avg_rating, rating_count in pagination.items:
        movie_dict = movie.to_dict()
        movie_dict["avg_rating"] = round(float(avg_rating), 2) if avg_rating else None
        movie_dict["rating_count"] = rating_count or 0
        movies.append(movie_dict)

    return jsonify({
        "movies":   movies,
        "total":    pagination.total,
        "page":     page,
        "per_page": per_page,
        "pages":    pagination.pages,
    })


@movies_bp.get("/<int:movie_id>")
def get_movie(movie_id: int):
    """GET /api/movies/<id> — 电影详情 + 平均评分"""
    movie = db.session.get(Movie, movie_id)
    if movie is None:
        return jsonify({"msg": "电影不存在"}), 404

    avg_rating = (
        db.session.query(func.avg(Rating.rating))
        .filter(Rating.movie_id == movie_id)
        .scalar()
    )
    rating_count = (
        db.session.query(func.count(Rating.id))
        .filter(Rating.movie_id == movie_id)
        .scalar()
    )

    result = movie.to_dict()
    result["avg_rating"]   = round(float(avg_rating), 2) if avg_rating else None
    result["rating_count"] = rating_count or 0
    return jsonify(result)


@movies_bp.get("/genres")
def list_genres():
    """GET /api/movies/genres — 返回所有类型列表"""
    rows = db.session.query(Movie.genres).filter(Movie.genres.isnot(None)).all()
    genres = set()
    for (g,) in rows:
        genres.update(g.split("|"))
    genres.discard("(no genres listed)")
    return jsonify(sorted(genres))
