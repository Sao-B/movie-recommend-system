from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from ..db import db
from ..models import User

auth_bp = Blueprint("auth", __name__)


@auth_bp.post("/register")
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"msg": "用户名和密码不能为空"}), 400
    if len(username) > 50:
        return jsonify({"msg": "用户名最长 50 个字符"}), 400
    if len(password) < 6:
        return jsonify({"msg": "密码至少 6 位"}), 400
    if len(password) > 128:
        return jsonify({"msg": "密码最长 128 位"}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({"msg": "用户名已存在"}), 409

    user = User(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return jsonify({"token": token, "user": user.to_dict()}), 201


@auth_bp.post("/login")
def login():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    user = User.query.filter_by(username=username).first()
    if not user or not user.check_password(password):
        return jsonify({"msg": "用户名或密码错误"}), 401

    token = create_access_token(identity=str(user.id))
    return jsonify({"token": token, "user": user.to_dict()})
