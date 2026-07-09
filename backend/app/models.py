from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from .db import db


class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def to_dict(self):
        return {"id": self.id, "username": self.username}


class Movie(db.Model):
    __tablename__ = "movies"

    movie_id = db.Column(db.Integer, primary_key=True)
    title    = db.Column(db.String(500), nullable=False)
    genres   = db.Column(db.String(500))
    year     = db.Column(db.SmallInteger)

    def to_dict(self):
        return {
            "movie_id": self.movie_id,
            "title":    self.title,
            "genres":   self.genres.split("|") if self.genres else [],
            "year":     self.year,
        }


class Rating(db.Model):
    __tablename__ = "ratings"

    id       = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id  = db.Column(db.Integer, nullable=False, index=True)
    movie_id = db.Column(db.Integer, db.ForeignKey("movies.movie_id"),
                         nullable=False, index=True)
    rating   = db.Column(db.Float, nullable=False)
    rated_at = db.Column(db.DateTime, default=datetime.utcnow)

    movie = db.relationship("Movie", backref="ratings")

    def to_dict(self):
        return {
            "id":       self.id,
            "user_id":  self.user_id,
            "movie_id": self.movie_id,
            "rating":   self.rating,
            "rated_at": self.rated_at.isoformat() if self.rated_at else None,
        }


class Recommendation(db.Model):
    __tablename__ = "recommendations"

    id               = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    user_id          = db.Column(db.Integer, nullable=False, index=True)
    movie_id         = db.Column(db.Integer, db.ForeignKey("movies.movie_id"),
                                 nullable=False)
    predicted_rating = db.Column(db.Float, nullable=False)
    rank             = db.Column(db.SmallInteger, nullable=False)
    generated_at     = db.Column(db.DateTime, default=datetime.utcnow)

    movie = db.relationship("Movie", backref="recommendations")

    def to_dict(self):
        return {
            "rank":             self.rank,
            "predicted_rating": round(self.predicted_rating, 4),
            "movie":            self.movie.to_dict() if self.movie else None,
        }
