from flask import Blueprint, request, jsonify
from sqlalchemy import func
from ..db import db
from ..models import Movie, Rating

movies_bp = Blueprint("movies", __name__)

_PAGE_SIZE = 20


@movies_bp.get("/")
def list_movies():
    """GET /api/movies?page=1&per_page=20&genre=Action&q=keyword&year=1995"""
    page     = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", _PAGE_SIZE, type=int))
    genre    = request.args.get("genre", "").strip()
    keyword  = request.args.get("q", "").strip()
    year     = request.args.get("year", type=int)

    query = Movie.query
    if genre:
        query = query.filter(Movie.genres.like(f"%{genre}%"))
    if keyword:
        query = query.filter(Movie.title.ilike(f"%{keyword}%"))
    if year:
        query = query.filter(Movie.year == year)

    pagination = query.order_by(Movie.movie_id).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify({
        "movies":   [m.to_dict() for m in pagination.items],
        "total":    pagination.total,
        "page":     page,
        "per_page": per_page,
        "pages":    pagination.pages,
    })


@movies_bp.get("/<int:movie_id>")
def get_movie(movie_id: int):
    """GET /api/movies/<id> — 电影详情 + 平均评分"""
    movie = Movie.query.get_or_404(movie_id)

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
