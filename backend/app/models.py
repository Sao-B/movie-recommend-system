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
    avatar_url    = db.Column(db.String(500))
    bio           = db.Column(db.Text)
    created_at    = db.Column(db.DateTime, default=_utcnow)
    role          = db.Column(db.String(16), nullable=False, default='user', server_default='user')

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        try:
            return check_password_hash(self.password_hash, password)
        except ValueError:
            # 哈希方法不被当前 werkzeug 支持（如历史遗留的 bcrypt），按密码错误处理而非 500
            return False

    def is_admin(self) -> bool:
        """检查用户是否为管理员"""
        return self.role == 'admin'

    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "avatar_url": self.avatar_url,
            "bio": self.bio,
            "created_at": _to_iso_utc(self.created_at)
        }


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


class UserRating(db.Model):
    """系统用户评分（canonical 存储，训练与在线推荐共用）。

    历史遗留：应用曾写 legacy `ratings` 表，005 迁移后统一到 `user_ratings`。
    """

    __tablename__ = "user_ratings"

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
    #: 用户手动编辑过正文的时间；**从未编辑为 NULL**（migrations/007 新增）。
    #: 判断「是否编辑过」不能用 updated_at——那一列在插入时就会被写成发表时刻，
    #: 用它做判据会把所有影评都显示成「已修改」。
    edited_at  = db.Column(db.DateTime)

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
            "edited_at":  _to_iso_utc(self.edited_at),
        }


class MovieRatingStats(db.Model):
    """MovieLens 评分五档分布（离线聚合产物）。

    ``movies`` 表只有 ``avg_rating`` / ``rating_count`` 两个标量，没有分档计数，
    而详情页「评分统计」需要 5/4/3/2/1 星各占多少。数据由
    ``scripts/build_ml_rating_stats.py`` 聚合 ``data/raw/ratings.csv`` 一次性写入，
    不随 ALS 训练变化。

    查询侧把它与本站 ``user_ratings`` 合并后展示，页面上不区分来源。
    """

    __tablename__ = "movie_rating_stats"

    movie_id     = db.Column(db.Integer, db.ForeignKey("movies.movie_id"),
                             primary_key=True)
    rating_count = db.Column(db.Integer, nullable=False)
    # 0.5 粒度的精确总和，用于与本站评分一起做加权平均
    # （movies.avg_rating 只有 2 位小数，精度不够）
    rating_sum   = db.Column(db.Numeric(14, 1), nullable=False)
    cnt_1        = db.Column(db.Integer, nullable=False, default=0)
    cnt_2        = db.Column(db.Integer, nullable=False, default=0)
    cnt_3        = db.Column(db.Integer, nullable=False, default=0)
    cnt_4        = db.Column(db.Integer, nullable=False, default=0)
    cnt_5        = db.Column(db.Integer, nullable=False, default=0)

    def star_counts(self):
        """返回 ``{5: n, 4: n, 3: n, 2: n, 1: n}``，供 rating_stats 合并使用。"""
        return {
            5: self.cnt_5 or 0,
            4: self.cnt_4 or 0,
            3: self.cnt_3 or 0,
            2: self.cnt_2 or 0,
            1: self.cnt_1 or 0,
        }
