from datetime import datetime, timezone
from werkzeug.security import generate_password_hash, check_password_hash
from .db import db


def _utcnow():
    """返回时区感知的 UTC 当前时间（替代已弃用的 datetime.utcnow）。"""
    return datetime.now(timezone.utc)


def _to_iso_utc(dt):
    """将 datetime 转换为带 UTC 时区的 ISO 格式字符串"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at    = db.Column(db.DateTime, default=_utcnow)
    role          = db.Column(db.String(16), nullable=False, default='user', server_default='user')

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def is_admin(self) -> bool:
        """检查用户是否为管理员"""
        return self.role == 'admin'

    def to_dict(self):
        return {"id": self.id, "username": self.username, "role": self.role}


class Movie(db.Model):
    __tablename__ = "movies"

    movie_id     = db.Column(db.Integer, primary_key=True)
    title        = db.Column(db.String(500), nullable=False)
    genres       = db.Column(db.String(500))
    year         = db.Column(db.SmallInteger)
    avg_rating   = db.Column(db.Numeric(3, 2))
    rating_count = db.Column(db.Integer, default=0)
    poster_url   = db.Column(db.String(500))
    overview     = db.Column(db.Text)
    director     = db.Column(db.String(500))
    writers      = db.Column(db.Text)
    cast         = db.Column(db.Text)
    countries    = db.Column(db.String(200))
    languages    = db.Column(db.String(200))
    release_date = db.Column(db.Date)
    runtime      = db.Column(db.SmallInteger)

    def to_dict(self):
        # 同时处理 None 和空字符串，避免 "".split("|") 返回 [""]
        genres_list = [g for g in (self.genres or "").split("|") if g]
        return {
            "movie_id":     self.movie_id,
            "title":        self.title,
            "genres":       genres_list,
            "year":         self.year,
            "avg_rating":   float(self.avg_rating) if self.avg_rating else None,
            "rating_count": self.rating_count or 0,
            "poster_url":   self.poster_url,
            "overview":     self.overview,
            "director":     self.director,
            "writers":      self.writers,
            "cast":         self.cast,
            "countries":    self.countries,
            "languages":    self.languages,
            "release_date": self.release_date.isoformat() if self.release_date else None,
            "runtime":      self.runtime,
        }


class Rating(db.Model):
    __tablename__ = "ratings"

    id       = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id  = db.Column(db.Integer, db.ForeignKey("users.id"),
                         nullable=False, index=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.movie_id"),
                         nullable=False, index=True)
    rating   = db.Column(db.Float, nullable=False)
    rated_at = db.Column(db.DateTime, default=_utcnow)

    movie = db.relationship("Movie", backref="ratings")

    def to_dict(self):
        return {
            "id":       self.id,
            "user_id":  self.user_id,
            "movie_id": self.movie_id,
            "rating":   self.rating,
            "rated_at": _to_iso_utc(self.rated_at),
        }


class Recommendation(db.Model):
    __tablename__ = "recommendations"

    id               = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    model_version    = db.Column(db.String(64), nullable=True, index=True)
    user_id          = db.Column(db.Integer, nullable=False, index=True)
    movie_id         = db.Column(db.Integer, db.ForeignKey("movies.movie_id"),
                                 nullable=False)
    predicted_rating = db.Column(db.Float, nullable=False)
    rank             = db.Column(db.SmallInteger, nullable=False)
    generated_at     = db.Column(db.DateTime, default=_utcnow)

    movie = db.relationship("Movie", backref="recommendations")

    def to_dict(self):
        return {
            "rank":             self.rank,
            "predicted_rating": round(self.predicted_rating, 4),
            "movie":            self.movie.to_dict() if self.movie else None,
        }


class ModelVersion(db.Model):
    __tablename__ = "model_versions"

    version      = db.Column(db.String(64), primary_key=True)
    rank         = db.Column(db.SmallInteger, nullable=False)
    reg_param    = db.Column(db.Float, nullable=False)
    status       = db.Column(db.String(16), nullable=False, index=True)
    generated_at = db.Column(db.DateTime, default=_utcnow)
    activated_at = db.Column(db.DateTime)


class ModelItemFactor(db.Model):
    __tablename__ = "model_item_factors"

    model_version = db.Column(
        db.String(64), db.ForeignKey("model_versions.version"), primary_key=True
    )
    movie_id     = db.Column(db.Integer, primary_key=True)
    factors      = db.Column(db.Text, nullable=False)
    generated_at = db.Column(db.DateTime, default=_utcnow)


class MovieComment(db.Model):
    __tablename__ = "movie_comments"

    id         = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id    = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    movie_id   = db.Column(db.Integer, db.ForeignKey("movies.movie_id"), nullable=False, index=True)
    content    = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_utcnow, nullable=False)
    updated_at = db.Column(db.DateTime, default=_utcnow, onupdate=_utcnow, nullable=False)

    user  = db.relationship("User", backref="comments")
    movie = db.relationship("Movie", backref="comments")

    def to_dict(self):
        return {
            "id":         self.id,
            "user_id":    self.user_id,
            "username":   self.user.username if self.user else None,
            "movie_id":   self.movie_id,
            "content":    self.content,
            "created_at": _to_iso_utc(self.created_at),
            "updated_at": _to_iso_utc(self.updated_at),
        }
