import os, config
from db import get_db, close_db
from flask import Flask, jsonify, render_template, request, session, redirect, url_for

app = Flask(__name__)
app.teardown_appcontext(close_db) 
secret_key = config.SECRET_KEY
if not secret_key:
    if os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError("生产环境必须设置 SECRET_KEY 环境变量")
    secret_key = "dev-only-fallback"   # 仅本地开发用

app.secret_key = secret_key

@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在", "path": request.path}), 404
    return render_template("404.html"), 404

@app.errorhandler(500)
def internal_error(e):
    # 服务器内部错误，不要暴露堆栈给用户
    return "服务器内部错误，请稍后重试", 500

@app.errorhandler(403)
def forbidden(e):
    return "没有权限访问", 403

@app.route("/")
def index():
    return render_template("index.html"),200

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        # 处理提交
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # ... 校验 ...
        if username != "admin" or password != "password":#校验失败
            return render_template("login.html", error="用户名或密码错误"), 401
        session["user_id"] = ...
        return redirect(url_for("index"))

    # GET：显示登录页
    return render_template("login.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
