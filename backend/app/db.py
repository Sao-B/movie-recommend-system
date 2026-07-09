from flask_sqlalchemy import SQLAlchemy

# 全局 db 实例，由 create_app() 通过 db.init_app(app) 绑定
db = SQLAlchemy()
