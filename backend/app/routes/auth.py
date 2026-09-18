import re

from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from ..account_rules import username_error
from ..db import db
from ..models import User

auth_bp = Blueprint("auth", __name__)

#: 登录标识是纯数字时才可能被当成用户 ID；只认 ASCII 数字（``\d`` 会连全角数字一起吃进来）
_DIGITS_RE = re.compile(r"^[0-9]+$")


def _find_user(identifier: str):
    """按「账号」找用户，找不到再按「用户 ID」找。

    顺序是先账号后 ID：万一真有人把账号起成 ``123456``，他自己那个账号优先，
    否则纯数字账号的持有者就永远登不进来了。
    """
    user = User.query.filter_by(username=identifier).first()
    if user is not None or not _DIGITS_RE.match(identifier):
        return user
    return db.session.get(User, int(identifier))


@auth_bp.post("/register")
def register():
    """POST /api/auth/register — 注册

    Body: ``{ "username": "...", "password": "..." }``。账号要符合
    ``app/account_rules.py`` 的规则（3-20 位，只允许字母 / 数字 / 下划线 / 连字符）。
    昵称初始与账号同名，之后在个人主页改——本接口不收 nickname。
    """
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"msg": "账号和密码不能为空"}), 400
    invalid = username_error(username)
    if invalid:
        return jsonify({"msg": invalid}), 400
    if len(password) < 6:
        return jsonify({"msg": "密码至少 6 位"}), 400
    if len(password) > 128:
        return jsonify({"msg": "密码最长 128 位"}), 400

    if User.query.filter_by(username=username).first():
        return jsonify({"msg": "账号已存在"}), 409

    user = User(username=username, nickname=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    token = create_access_token(identity=str(user.id))
    return jsonify({"token": token, "user": user.to_dict()}), 201


@auth_bp.post("/login")
def login():
    """POST /api/auth/login — 登录

    Body: ``{ "username": "...", "password": "..." }``。``username`` 这个字段里
    放**账号或自己的用户 ID** 都行（个人主页的「ID: 3」就是它）——纯数字时先按账号找、
    找不到再按 ID 找。
    """
    data = request.get_json(silent=True) or {}
    identifier = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    user = _find_user(identifier) if identifier else None
    if not user or not user.check_password(password):
        # 文案统一，避免通过报错信息枚举账号
        return jsonify({"msg": "账号或密码错误"}), 401

    token = create_access_token(identity=str(user.id))
    return jsonify({"token": token, "user": user.to_dict()})
