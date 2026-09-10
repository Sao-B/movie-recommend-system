from functools import wraps
from flask import Blueprint, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from ..models import User

admin_bp = Blueprint('admin', __name__)

def admin_required(fn):
    """管理员权限装饰器：验证当前用户是否为管理员"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()
        user_id = get_jwt_identity()
        user = User.query.get(int(user_id))
        if not user or not user.is_admin():
            return jsonify({"msg": "需要管理员权限"}), 403
        return fn(*args, **kwargs)
    return wrapper

@admin_bp.route('/test', methods=['GET'])
@admin_required
def test_admin():
    """测试接口：验证管理员权限装饰器"""
    return jsonify({"msg": "管理员权限验证成功"})
