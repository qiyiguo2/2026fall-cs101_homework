import os
import json
import re
import uuid
from urllib.parse import urlsplit
from functools import wraps

import config
from db import get_db, close_db, resolve_recording_dir
from flask import (
    Flask,
    abort,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from services import auth, grading, practice, registration

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.teardown_appcontext(close_db)

app.secret_key = config.SECRET_KEY
if not os.environ.get("SECRET_KEY"):
    app.logger.warning(
        "未设置 SECRET_KEY，已改用随机密钥；进程重启后所有登录状态会失效。"
    )

# 上传体积上限由应用自己把住，不能只指望前置的 nginx client_max_body_size：
# 换一台前面没有 nginx 的机器（本地调试、直接 gunicorn、以后换反代）就是裸奔，
# 而一个 20MB 的录音包在 1 核机上足以把内存和磁盘一起顶穿。
# 超限后 Werkzeug 在读取请求体时就抛 413，body 根本不会落地，见下面的 errorhandler。
app.config["MAX_CONTENT_LENGTH"] = int(config.MAX_UPLOAD_MB * 1024 * 1024)

# 登录 cookie 的属性。Flask 默认只有 HttpOnly：
# - SameSite=Lax：别的网站发来的跨站 POST 不带这枚 cookie，挡掉「外站一个表单
#   就替学生改设置、改用户名」这类 CSRF。站内表单、从外链点进来的 GET 都不受影响。
#   各浏览器对「没写 SameSite」的默认处理不一致（Chrome 当 Lax，且签发后 2 分钟内放行 POST），
#   所以要显式写出来，不依赖默认。
# - Secure：见 config.SESSION_COOKIE_SECURE 的说明，生产开、开发关。
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = config.SESSION_COOKIE_SECURE

RECORDING_EXTENSIONS = {"webm", "ogg", "wav", "mp4", "m4a", "mp3", "mpeg"}

# 近似音两种题型走各自的出题函数，回包结构完全一样
VOWEL_BUILDERS = {
    "word": practice.build_vowel_questions,
    "sentence": practice.build_sentence_questions,
}

# 录音文件名一律由后端生成：u<用户 id>_<12 位十六进制>.<扩展名>
RECORDING_NAME_RE = re.compile(r"^u(\d+)_[0-9a-f]{12}\.[A-Za-z0-9]{1,8}$")


# ---------------------------------------------------------------- 工具

def recording_dir():
    """录音目录在文件系统上的绝对路径（RECORDING_DIR 相对/绝对都接受）。"""
    return resolve_recording_dir()


def recording_prefix():
    """录音路径存进库、以及从库里比对时统一用这个前缀：永远相对项目根。

    这是 RECORDING_DIR 那个坑的正解。以前四处直接拼 config.RECORDING_DIR，
    一旦部署时填成绝对路径（/opt/english-practice/app/data/recordings），
    库里就存成绝对路径，而 audio_url() 比对前会先 lstrip("/")，
    前缀怎么都对不上，最后落到 `return "/" + rel` 那个兜底分支 ——
    结果页的录音链接变成 /opt/english-practice/... 这种 404，
    而且只在生产机上出现，本地永远测不出来。
    """
    target = recording_dir()
    try:
        rel = os.path.relpath(target, BASE_DIR)
    except ValueError:      # Windows 上跨盘符时 relpath 会抛，退回原样
        rel = target
    return rel.replace("\\", "/").rstrip("/")


def recording_owner(filename):
    """从文件名里取出后端当初登记的归属 id；文件名不合规则返回 None。"""
    name = str(filename or "").replace("\\", "/").rsplit("/", 1)[-1]
    match = RECORDING_NAME_RE.match(name)
    return int(match.group(1)) if match else None


def own_recording_path(rel_path, user_id):
    """只承认后端为当前用户生成的录音文件。

    提交成绩时 recording_path 来自前端，不加校验就能把别人的录音路径写进
    自己的记录里回放，也允许塞进任意字符串。不匹配时按「没有录音」处理。
    """
    if not rel_path:
        return None
    name = str(rel_path).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if recording_owner(name) != user_id:
        return None
    return f"{recording_prefix()}/{name}"


MISSING = object()


def as_count(value, fallback):
    """把前端传来的题量规整成 1..MAX_QUESTION_COUNT，非法值抛 ValueError 让接口回 400。

    键根本没出现时用 fallback；显式传 null 是前端的 bug，不能被默认值悄悄掩盖。
    """
    if value is MISSING:
        return fallback
    if value is None:
        raise ValueError("题目数量不能是 null")
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise ValueError("题目数量必须是数字")
    if number < 1:
        raise ValueError("题目数量至少为 1")
    return min(number, config.MAX_QUESTION_COUNT)

def current_user():
    if "user_id" not in g:
        g.user_id = session.get("user_id")
        g.user = auth.get_user(g.user_id) if g.user_id else None
        if g.user_id and not g.user:      # 账号被删了，清掉过期会话
            session.pop("user_id", None)
            g.user_id = None
    return g.user


def refresh_current_user():
    """改完资料后重新查库，让本次请求的导航栏也显示新值。"""
    g.pop("user_id", None)
    g.pop("user", None)
    return current_user()


# ------------------------------------------------------------------ 运行模式
# config.APP_MODE 决定这台站是「要登录、数据进服务器」还是「不登录、数据留本机」。
# 后端只在这里判断一次，其他地方一律调 is_local() / practice_profile()，
# 免得「哪些路由认本地模式」散落在十几个函数里各写各的口径。

def is_local():
    """当前是否跑在本地模式（不登录，练习数据全在浏览器里）。"""
    return config.APP_MODE == "local"


def local_profile():
    """本地模式下代替账号记录的那份合成档案。

    练习页的输入框默认值、rhythm_defaults() / overlap_default() 都按 dict 取值，
    这里给一份只用 config 默认值的 dict，它们就会自动退回「没人设置过」的那条路径
    （rhythm_defaults 里 stored is None → 用 config 的默认），不需要再加分支。

    id 是 None：真有人拿它去写库就是 bug，所以写库接口在 local 模式下根本不会被调用
    （见 api_create_session 的闸门），而不是靠这个 None 兜底。
    """
    return {
        "id": None,
        "username": "guest",
        "nickname": "",
        "created_at": None,
        "default_count": config.DEFAULT_QUESTION_COUNT,
        "time_limit": config.DEFAULT_TIME_LIMIT,
        "voice": None,
    }


def practice_profile():
    """出题和练习页默认值该用谁的设置：登录用户用他的账号，本地模式用 config 默认。

    注意这**不是**「当前用户」：只有两处非用不可的地方用它（练习页默认值、
    近似音每题限时）。凡是涉及归属、写库、鉴权的地方仍然只看 current_user()。
    """
    if is_local():
        return local_profile()
    return current_user()


@app.context_processor
def inject_user():
    # data_mode / local_mode 是给模板切文案和导航用的：本地模式的导航栏不该出现
    # 「登录 / 注册」，也不该出现「账号」这种会让人以为数据存到服务器上的词。
    return {
        "current_user": current_user(),
        "nav_active": request.endpoint,
        "data_mode": config.APP_MODE,
        "local_mode": is_local(),
    }


def safe_next(target):
    """只允许跳回本站相对路径，避免开放重定向。

    只看「以 / 开头、不以 // 开头」不够：/<tab>/evil.example.com 两条都满足，
    但浏览器解析 URL 时会先删掉 tab、换行，剩下 //evil.example.com 就跳出站了。
    反斜杠同理（部分浏览器把 “/ 反斜杠 evil”当成 //evil）。所以控制字符和反斜杠一律拒绝，
    再用 urlsplit 确认既没有协议也没有主机。
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in target):
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        # 本地模式：练习页不需要身份，服务器也没有可校验的会话。
        # 这里放行只是「不必登录就能进页面」，写库的接口另有闸门（见 api_create_session）。
        if is_local() or current_user():
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"error": "请先登录"}), 401
        return redirect(url_for("login", next=request.path))

    return wrapped


def api_login_required(view):
    """接口专用：只接受 session 登录，未登录返回 401 JSON。"""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if is_local() or current_user():
            return view(*args, **kwargs)
        return jsonify({"error": "请先登录"}), 401

    return wrapped


def local_mode_refuse(what):
    """本地模式下把写数据的接口挡回去。

    正常的前端在本地模式根本不会发这些请求（PracticeStore 走 IndexedDB），
    来的是没更新的旧 JS、别的站点，或者有人手动 curl。与其默默写进一张没有
    归属的表（user_id 为 NULL，账号页查询按 user_id 过滤，等于永远看不见的数据垃圾），
    不如直接拒绝并说明原因。出题用的只读接口不受影响。
    """
    return jsonify({
        "error": "本站当前运行在本地模式：练习记录和录音只存在你的设备里，"
                 "服务器不接收也不保存。请刷新页面重新加载脚本。",
        "mode": "local",
        "unsupported": what,
    }), 409


def audio_url(rel_path):
    """把数据库里的相对路径转成可访问 URL。"""
    if not rel_path:
        return None
    rel = rel_path.replace("\\", "/").lstrip("/")
    if rel.startswith("static/"):
        return url_for("static", filename=rel[len("static/"):])
    rec_prefix = recording_prefix() + "/"
    if rel.startswith(rec_prefix):
        # 录音不在 static 下，必须走 /recordings/ 路由（那里有归属校验）
        return url_for("recordings", filename=rel[len(rec_prefix):])
    return "/" + rel


def json_body():
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------- 错误页

@app.errorhandler(404)
def page_not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "接口不存在", "path": request.path}), 404
    return render_template("404.html"), 404


@app.errorhandler(500)
def internal_error(e):
    return "服务器内部错误，请稍后重试", 500


@app.errorhandler(403)
def forbidden(e):
    return "没有权限访问", 403


@app.errorhandler(413)
def payload_too_large(e):
    """超过 config.MAX_UPLOAD_MB 时，Werkzeug 在读请求体阶段就抛到这里。

    回 JSON 而不是默认那张 HTML 错误页：前端这条链路全是 fetch，
    拿到 HTML 只会显示「提交失败」，学生根本不知道是自己录音太长。
    最常见的原因是 iOS Safari 走的是未压缩 WAV 兜底路径（44.1kHz 16bit，
    一分钟就是 10MB 上下），一题读久了就会顶到上限。
    """
    limit = int(config.MAX_UPLOAD_MB)
    detail = f"上传内容超过 {limit}MB，这一题的录音太长了，请重新录短一些"
    if request.path.startswith("/api/"):
        return jsonify({"error": detail}), 413
    return detail, 413


# ---------------------------------------------------------------- 静态资源

@app.route("/js/<path:filename>")
def scripts_js(filename):
    """前端 JS 放在 scripts/ 目录，只允许取 .js 文件。"""
    if not filename.endswith(".js"):
        return "Forbidden", 403
    return send_from_directory(os.path.join(BASE_DIR, "scripts"), filename)


@app.route("/recordings/<path:filename>")
def recordings(filename):
    """用户上传的朗读音频，不放在 static 下，避免被当成源码目录。

    这里必须做归属校验。以前是裸 `send_from_directory`，防线只有「文件名里那
    12 位十六进制猜不到」—— 可 uuid 不是机密：结果页的 HTML 里就写着完整 URL，
    任何人拿到链接（截图、分享、浏览器历史、访问日志）就能永久回放别人的朗读，
    而录音里往往有学生自报家门。写入侧 own_recording_path() 早就在比归属了，
    读取侧不比对等于是半个漏洞。

    不是本人一律 404，不用 403 —— 403 等于承认「这个文件确实存在」。
    """
    name = filename.replace("\\", "/")
    if "/" in name:                       # 录音是平铺的，带子目录的都不是我们发的
        abort(404)
    user = current_user()
    if not user or recording_owner(name) != user["id"]:
        abort(404)
    return send_from_directory(recording_dir(), name)


# ---------------------------------------------------------------- 页面路由

@app.route("/")
def index():
    groups = practice.list_vowel_groups()
    word_count = get_db().execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"]
    return render_template("index.html", vowel_groups=groups, word_count=word_count)


@app.route("/login", methods=["GET", "POST"])
def login():
    if is_local():
        # 旧书签 / 导航缓存还会指到 /login：给一句人话说明，而不是让他对着登录框猜
        return mode_notice()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        try:
            user = auth.verify_login(username, password)
        except auth.HashQueueFull:
            # 哈希闸门排不下 = 登录的人实在太多。快速回一句话让人重试，
            # 比让请求挂在那儿、最后被 nginx 的 60 秒超时切成 504 好得多。
            return render_template(
                "login.html", error="同时登录的人有点多，请过几秒再试一次"), 503
        except auth.AuthError as exc:
            return render_template("login.html", error=str(exc)), 401
        session["user_id"] = user["id"]
        return redirect(safe_next(request.values.get("next")) or url_for("practice"))

    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if is_local():
        return mode_notice()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        nickname = request.form.get("nickname", "")
        # 码原样回显：填错了不用重敲，学生手机上一个一个字补很难受
        invite_code = request.form.get("invite_code", "").strip()
        # 提交时的口径以服务端为准，不看前端有没有把输入框渲染出来：
        # 拿着 open 状态下缓存的表单 POST 到 invite / closed 状态，一样会被挡。
        gate = registration.state()

        def fail(message, status=400):
            return render_template("register.html", error=message, gate=gate,
                                   username=username, nickname=nickname,
                                   invite_code=invite_code), status

        if not gate["form_open"]:
            return fail(gate["hint"], 403)
        if password != confirm:
            return fail("两次输入的密码不一致")
        try:
            user = auth.register(username, password, nickname, invite_code)
        except auth.HashQueueFull:
            return fail("同时注册的人有点多，请过几秒再试一次", 503)
        except auth.AuthError as exc:
            return fail(str(exc))
        session["user_id"] = user["id"]
        return redirect(url_for("practice"))

    return render_template("register.html", gate=registration.state())


@app.route("/logout", methods=["POST", "GET"])
def logout():
    if is_local():
        # 本地模式没有会话可退出。这里也不能调 session.pop 之后 redirect 到首页——
        # 首页在本地模式下照样能练，来回跳只会让人以为「退出去又自动登进来了」。
        return mode_notice()
    # 只收 POST：GET 能退出的话，外站放一张 <img src=".../logout"> 就能把人踢下线，
    # SameSite=Lax 对顶层 GET 不设防。导航栏里的退出按钮本来就是 POST 表单。
    # GET 仍留在 methods 里，是为了本地模式那条说明页（上面）照旧能打开。
    if request.method != "POST":
        abort(405)
    session.pop("user_id", None)
    return redirect(url_for("index"))


def mode_notice():
    """本地模式下替代登录 / 注册 / 退出这三个入口的说明页。"""
    return render_template("mode_notice.html"), 200


@app.route("/practice", endpoint="practice")
@login_required
def practice_page():
    return render_template("practice.html", vowel_groups=practice.list_vowel_groups())


@app.route("/practice/vowel")
@login_required
def practice_vowel():
    return render_template(
        "practice_vowel.html",
        vowel_groups=practice.list_vowel_groups(),
        modes=practice.VOWEL_MODES,
        defaults={
            "count": practice_profile()["default_count"],
            "time_limit": practice_profile()["time_limit"],
            # 没有 voice：近似音每题随机抽男声/女声，账号里的旧设置不再参与出题
        },
    )


def _fmt_seconds(value):
    """输入框里的数：5.0 写成 5、1.50 写成 1.5。挂一串 .0 既难看也更占地方。"""
    return f"{round(float(value), 2):g}"


def rhythm_defaults(user=None):
    """看读译 / 听读写那三个时长输入框的 min / max / 默认值。

    边界只有 `config.RHYTHM_FIELDS` 这一份；**默认值优先取账号里存的那份**（在 /settings 改），
    没存过就退回 config 的默认值，存了个出格的值（比如以后把上限调小了）就夹回边界。
    库里连这一列都还没有（老库没跑 scripts/init_db.py 补列）也当「没存过」处理，
    练习页照常能开，只是设置保存不下来。

    模板负责渲染边界，tworounds.js 再按输入框的 min/max 夹一遍，
    不会出现「页面允许 999 秒而脚本按 5 秒跑」这种两边不一致。
    """
    out = {}
    for name, (column, (lo, hi, default)) in config.RHYTHM_FIELDS.items():
        value = default
        stored = user.get(column) if isinstance(user, dict) else None
        if stored is not None:
            try:
                value = max(lo, min(hi, float(stored)))
            except (TypeError, ValueError):
                value = default          # 库里是脏值就当没存过，别把练习页弄白屏
        out[name] = {"min": lo, "max": hi, "value": _fmt_seconds(value)}
    return out


def overlap_default(user=None):
    """听读写第一轮「边播边录」这个开关的用户默认值；没存过按以前的默认（开着）。"""
    if not isinstance(user, dict):
        return True
    stored = user.get("overlap_record")
    return True if stored is None else bool(stored)


# 会话 params 里存的是毫秒（JS 那边只用毫秒），结果页要换回人能读的秒
RHYTHM_LABELS = {
    "round1_ms": "第一轮（秒）",
    "round2_ms": "第二轮（秒）",
    "round_gap_ms": "题间停顿（秒）",
}

# 开关项不是时长，不能跟着除以 1000，单独给一套中文值
SWITCH_LABELS = {
    "overlap_record": ("第一轮录音方式", {True: "边播边录", False: "先听后录"}),
}


def humanize_params(params):
    """把 round1_ms=1000 这类内部字段渲染成「第一轮（秒）= 1」。"""
    out = {}
    for key, value in params.items():
        label = RHYTHM_LABELS.get(key)
        if label is not None:
            try:
                out[label] = f"{round(float(value) / 1000, 2):g}"
            except (TypeError, ValueError):
                out[label] = value      # 老数据里万一存了字符串，原样显示而不是报错
            continue
        if key in SWITCH_LABELS:
            name, mapping = SWITCH_LABELS[key]
            out[name] = mapping.get(bool(value), str(value))
            continue
        out[key] = value
    return out


@app.route("/practice/read-translate")
@login_required
def practice_read_translate():
    user = practice_profile()
    return render_template(
        "practice_read_translate.html",
        defaults={"count": user["default_count"]},
        rhythm=rhythm_defaults(user),
    )


@app.route("/practice/listen-spell")
@login_required
def practice_listen_spell():
    user = practice_profile()
    return render_template(
        "practice_listen_spell.html",
        defaults={"count": user["default_count"]},
        rhythm=rhythm_defaults(user),
        overlap=overlap_default(user),
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if is_local():
        # 本地模式没有账号可写：这张表单由 scripts/practice-store.js 的 LocalSettings 接管，
        # 存进本机 localStorage。
        # 服务器只负责把表单和边界画出来（值来自 config 默认），而且这里任何情况下都不碰数据库
        # —— 万一有人绕过 JS 直接 POST 到 /settings，也只会重新拿到同一张空表单。
        # 用同一份 settings.html 而不是另写一个模板：字段名和边界只有一份，
        # 以后加一项不会出现「服务器模式能存、本地模式忘加」这种走偏。
        return render_template("settings.html", user=local_profile(), message=None,
                               rhythm=rhythm_defaults(None),
                               overlap=overlap_default(None))

    message = None
    if request.method == "POST":
        try:
            auth.update_settings(
                current_user()["id"],
                default_count=request.form.get("default_count"),
                time_limit=request.form.get("time_limit"),
                voice=request.form.get("voice"),
                # 三段时长：和练习页用同一套 name，越界由 update_settings 夹回 config 的边界
                round1_seconds=request.form.get("round1_seconds"),
                round2_seconds=request.form.get("round2_seconds"),
                round_gap_seconds=request.form.get("round_gap"),
                # 复选框不勾就整个字段不提交，必须补一个 "0"，否则「关掉边播边录」存不下去
                overlap_record=request.form.get("overlap_record", "0"),
            )
            message = "设置已保存"
        except auth.AuthError as exc:
            message = f"保存失败：{exc}"
        refresh_current_user()
    user = auth.get_user(current_user()["id"])
    return render_template("settings.html", user=user, message=message,
                           rhythm=rhythm_defaults(user),
                           overlap=overlap_default(user))


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if is_local():
        # 本地模式没有账号，这一栏改叫「我的数据」：列出本机存过的练习、导出导入、清空。
        # 列表内容由 scripts/local-data.js 从 IndexedDB 现取，服务器一份数据都不掌握。
        return render_template("local_data.html")

    message = None
    error = None
    if request.method == "POST":
        action = request.form.get("action")
        uid = current_user()["id"]
        try:
            if action == "username":
                new_name = auth.change_username(uid, request.form.get("new_username"))
                message = f"用户名已改为 {new_name}"
            elif action == "password":
                auth.change_password(
                    uid, request.form.get("old_password"), request.form.get("new_password")
                )
                message = "密码已修改"
            else:
                error = "未知操作"
        except auth.AuthError as exc:
            error = str(exc)
        except auth.HashQueueFull:
            # 哈希闸门（config.MAX_CONCURRENT_HASHES）排队超时，不是用户输错了什么，
            # 措辞别和「原密码不正确」混在一起，免得人去反复试密码。
            error = "同时操作的人有点多，请过几秒再试一次"
        refresh_current_user()

    user = auth.get_user(current_user()["id"])
    recent = get_db().execute(
        "SELECT id, practice_type, total, correct_count, wrong_count, created_at "
        "FROM practice_sessions WHERE user_id = ? AND finished = 1 "
        "ORDER BY id DESC LIMIT 10",
        (user["id"],),
    ).fetchall()
    return render_template("account.html", user=user, message=message,
                           error=error, recent_sessions=recent)


@app.route("/help")
def help_page():
    # 帮助页要告诉学生词库有多大，数字从库里现取，加词之后不用改文案
    word_count = get_db().execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"]
    return render_template("help.html", word_count=word_count)


@app.route("/result/<int:session_id>")
@login_required
def result_page(session_id):
    if is_local():
        # 本地模式的结果页只是一张空壳：题目、作答、录音全在浏览器 IndexedDB 里，
        # 由 scripts/local-result.js 按 URL 里这个 id 取出来画。
        # 这里不查库，也就保证了一个旧的服务器会话 id 不会在别人机器上翻出内容。
        return render_template("result_local.html", session_id=session_id)

    row = get_db().execute(
        "SELECT * FROM practice_sessions WHERE id = ? AND user_id = ?",
        (session_id, current_user()["id"]),
    ).fetchone()
    if not row:
        return render_template("404.html"), 404

    records = get_db().execute(
        "SELECT seq, question_json, user_answer, is_correct, recording_path "
        "FROM practice_records WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    items = []
    for rec in records:
        item = json.loads(rec["question_json"])
        item["seq"] = rec["seq"]
        item["user_answer"] = rec["user_answer"]
        item["is_correct"] = bool(rec["is_correct"])
        item["recording_url"] = audio_url(rec["recording_path"]) or ""
        # Jinja 会把 None 渲染成字符串 "None"，模板里的 if (url) 判断会被骗过
        item["audio_url"] = item.get("audio_url") or ""
        items.append(item)

    try:
        params = json.loads(row["params_json"] or "{}")
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}     # 早期脏数据里 params 可能是字符串，避免结果页整页 500
    return render_template("result.html", session_row=dict(row),
                           params=humanize_params(params), items=items)


# ---------------------------------------------------------------- 接口

@app.post("/api/vowel/questions")
@api_login_required
def api_vowel_questions():
    data = json_body()
    mode = data.get("mode", "word")
    builder = VOWEL_BUILDERS.get(mode)
    if builder is None:
        return jsonify({"error": "未知的练习类型，请选单词练习或句子练习"}), 400
    if not is_local() and data.get("session_id") is None:
        # 服务器模式下这里从不下发答案，题目必须挂在一场会话上，作答时才能换回对错
        return jsonify({"error": "缺少 session_id：页面脚本可能是旧版本，请刷新页面后重新开始"}), 400
    try:
        requested = as_count(data.get("count", MISSING), 10)
        questions = builder(
            data.get("group_key", ""),
            requested,
            # 不再按账号设置固定性别：默认随机，只有显式传 f/m 才锁定
            data.get("voice", practice.DEFAULT_VOWEL_VOICE),
        )
    except (ValueError, TypeError) as exc:
        return jsonify({"error": str(exc)}), 400

    for q in questions:
        q["audio_url"] = audio_url(q["audio"])
        q.pop("audio", None)
    refused = register_questions(
        data, ("vowel",), [grading.snapshot_vowel(q, mode) for q in questions])
    if refused:
        return refused
    if not is_local():
        # 答案留在服务器的快照里：学生作答后经 /api/vowel/answer 才换回对错。
        # 音频也换成按题号取的地址，原来的文件名（如 n.3-sheep.f.wav）里就写着单词。
        sid = int(data["session_id"])
        for q in questions:
            q.pop("answer", None)
            q["audio_url"] = url_for("practice_audio", session_id=sid, seq=q["seq"])
    return jsonify({
        "questions": questions,
        "mode": mode,
        "time_limit": practice_profile()["time_limit"],
        "requested": requested,
        "actual": len(questions),
    })


@app.get("/api/words/all")
@api_login_required
def api_words_all():
    """完整词表，只用于前端「音频导入」的文件名匹配。"""
    rows = get_db().execute("SELECT word FROM words ORDER BY word").fetchall()
    return jsonify({"words": [{"word": r["word"]} for r in rows]})


@app.post("/api/words")
@api_login_required
def api_words():
    data = json_body()
    try:
        requested = as_count(data.get("count", MISSING), config.DEFAULT_QUESTION_COUNT)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    words, bank_size = practice.random_words(requested)
    for w in words:
        # practice.random_words 已经按题随机挑好性别，这里只把相对路径换成 URL
        w["audio_url"] = audio_url(w.pop("audio"))
    session_type = registered_type(data)
    refused = register_questions(
        data, ("read-translate", "listen-spell"),
        [grading.snapshot_word(i, w, session_type) for i, w in enumerate(words, start=1)])
    if refused:
        return refused
    # actual < requested 说明词库不够，前端要提示用户实际只练了 actual 题
    return jsonify({
        "words": words,
        "requested": requested,
        "actual": len(words),
        "bank_size": bank_size,
    })


@app.post("/api/practice/session")
@api_login_required
def api_create_session():
    if is_local():
        return local_mode_refuse("practice_session")
    data = json_body()
    ptype = data.get("practice_type")
    if ptype not in practice.PRACTICE_TYPES:
        return jsonify({"error": "未知的练习类型"}), 400
    params = data.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return jsonify({"error": "params 必须是对象"}), 400
    db = get_db()
    cur = db.execute(
        "INSERT INTO practice_sessions(user_id, practice_type, params_json) VALUES(?,?,?)",
        (current_user()["id"], ptype, json.dumps(params, ensure_ascii=False)),
    )
    db.commit()
    return jsonify({"session_id": cur.lastrowid})


def already_finished(session_id):
    """已交卷的会话再提交：409，同时给出结果页地址。

    最常见的来源不是作弊，而是「服务器已经存好、响应在路上丢了」后前端重试，
    所以带上 result_url，前端据此直接跳去结果页，而不是报错。
    """
    return jsonify({
        "error": "这场练习已经交过卷了，成绩不能再改",
        "result_url": url_for("result_page", session_id=session_id),
    }), 409


# ---------------------------------------------------------------- 出题登记
# 服务器模式下成绩由后端判（services/grading.py）：出题接口带 session_id 时，
# 把这场发出去的题连同标准答案记进 practice_sessions.questions_json，交卷时按它重算。
# 本地模式不写库，session_id 是浏览器 IndexedDB 的键，这里直接忽略。

def _owned_open_session(session_id):
    """当前用户名下、还没交卷的会话；找不到返回 None。"""
    try:
        sid = int(session_id)
    except (TypeError, ValueError):
        return None
    return get_db().execute(
        "SELECT id, practice_type, finished, questions_json FROM practice_sessions "
        "WHERE id = ? AND user_id = ?",
        (sid, current_user()["id"]),
    ).fetchone()


def registered_type(data):
    """/api/words 两种练习共用：快照里的题型取会话自己的类型，不信前端。"""
    if is_local() or data.get("session_id") is None:
        return None
    row = _owned_open_session(data.get("session_id"))
    return row["practice_type"] if row else None


def register_questions(data, allowed_types, snapshots):
    """把发出去的题记到会话上。成功（或无需登记）返回 None，否则返回错误响应。

    不带 session_id 的请求照常出题、不登记：这样的会话交卷时会被拒绝（见 api_finish_session），
    所以不登记不会变成绕过判分的口子。一场练习只登记一次，第二次出题回 409 ——
    前端每点一次「开始练习」都会新建会话，正常流程不会撞上。
    """
    if is_local() or data.get("session_id") is None:
        return None
    row = _owned_open_session(data.get("session_id"))
    if not row:
        return jsonify({"error": "练习会话不存在"}), 404
    if row["practice_type"] not in allowed_types:
        return jsonify({"error": "这场练习的类型和所出的题对不上"}), 400
    if row["finished"]:
        return already_finished(row["id"])
    db = get_db()
    claimed = db.execute(
        "UPDATE practice_sessions SET questions_json = ? "
        "WHERE id = ? AND questions_json IS NULL AND finished = 0",
        (json.dumps(snapshots, ensure_ascii=False), row["id"]),
    ).rowcount
    db.commit()
    if not claimed:
        return jsonify({"error": "这场练习已经出过题了，请重新开始一场"}), 409
    return None


def _issued_question(session_id, seq):
    """当前用户名下某场练习登记过的第 seq 题，连同会话行一起返回；找不到返回 (None, None)。"""
    row = _owned_open_session(session_id)
    if not row or row["questions_json"] is None:
        return row, None
    for q in json.loads(row["questions_json"]):
        if q.get("seq") == seq:
            return row, q
    return row, None


@app.get("/api/practice/audio/<int:session_id>/<int:seq>")
@api_login_required
def practice_audio(session_id, seq):
    """按题号播近似音的音频，地址里不带文件名，作答前看网络面板也看不出播的是哪个词。"""
    if is_local():
        abort(404)
    _, question = _issued_question(session_id, seq)
    url = (question or {}).get("audio_url") or ""
    prefix = (app.static_url_path or "/static").rstrip("/") + "/"
    if not url.startswith(prefix):
        abort(404)
    response = send_from_directory(app.static_folder, url[len(prefix):])
    # 只给本人缓存：同一题的「重播」不用再下载，但共享缓存不能把它当公共资源存
    response.headers["Cache-Control"] = "private, max-age=3600"
    return response


@app.post("/api/vowel/answer")
@api_login_required
def api_vowel_answer():
    """近似音逐题判分：交上一题的选项，换回对错和标准答案。

    一题只认第一次提交（practice_answers 的主键挡住第二次）：答案一旦换出去，
    再交别的选项也改不了这一题，交卷时用的也是这里锁定的那个。
    """
    if is_local():
        return local_mode_refuse("vowel_answer")
    data = json_body()
    try:
        seq = int(data.get("seq"))
    except (TypeError, ValueError):
        return jsonify({"error": "题号不是数字"}), 400
    row, question = _issued_question(data.get("session_id"), seq)
    if not row:
        return jsonify({"error": "练习会话不存在"}), 404
    if row["practice_type"] != "vowel":
        return jsonify({"error": "这场练习不是近似音"}), 400
    if row["finished"]:
        return already_finished(row["id"])
    if not question:
        return jsonify({"error": f"这场练习里没有第 {seq} 题"}), 404

    choice = grading.vowel_choice(data.get("choice"))
    db = get_db()
    db.execute(
        "INSERT OR IGNORE INTO practice_answers(session_id, seq, user_answer) VALUES(?,?,?)",
        (row["id"], seq, None if choice is None else str(choice)),
    )
    db.commit()
    locked = db.execute(
        "SELECT user_answer FROM practice_answers WHERE session_id = ? AND seq = ?",
        (row["id"], seq),
    ).fetchone()["user_answer"]
    locked_choice = grading.vowel_choice(locked)
    return jsonify({
        "seq": seq,
        "choice": locked_choice,
        "correct": grading.judge(question, locked_choice),
        "answer": question["answer"],
    })


@app.post("/api/practice/finish")
@api_login_required
def api_finish_session():
    """练习结束时一次性提交全部作答记录。

    前端只被采信「第几题、答了什么、录音是哪个文件」：题目内容和标准答案取出题时
    登记的快照，对错由 services/grading.py 重算，前端传来的 is_correct 和 question 一律不用。
    没交上来的题按无回答算错，总题数等于发出去的题数——只交答对的那几题拿不到满分。
    """
    if is_local():
        return local_mode_refuse("practice_finish")
    data = json_body()
    session_id = data.get("session_id")
    records = data.get("records")
    uid = current_user()["id"]

    if not isinstance(records, list):
        return jsonify({"error": "records 必须是数组"}), 400
    if len(records) > config.MAX_QUESTION_COUNT:
        return jsonify({"error": f"单次最多提交 {config.MAX_QUESTION_COUNT} 题"}), 400

    db = get_db()
    row = db.execute(
        "SELECT id, finished, questions_json FROM practice_sessions WHERE id = ? AND user_id = ?",
        (session_id, uid),
    ).fetchone()
    if not row:
        return jsonify({"error": "练习会话不存在"}), 404
    if row["finished"]:
        return already_finished(session_id)
    if row["questions_json"] is None:
        # 多半是浏览器还缓存着改版前的脚本：出题时没带 session_id，服务器不知道发过哪些题
        return jsonify({"error": "服务器上没有这场练习的出题记录，无法判分。"
                                 "请刷新页面（必要时强制刷新）后重新开始一场。"}), 409
    issued = {q["seq"]: q for q in json.loads(row["questions_json"])}

    # 先把所有记录校验完再动数据库：边校验边写会让一条脏数据把已有成绩删掉后 500
    answers = {}
    for index, rec in enumerate(records, start=1):
        if not isinstance(rec, dict):
            return jsonify({"error": f"第 {index} 条记录格式不正确"}), 400
        try:
            seq = int(rec.get("seq", index))
        except (TypeError, ValueError):
            return jsonify({"error": f"第 {index} 条记录的题号不是数字"}), 400
        if seq not in issued:
            return jsonify({"error": f"第 {index} 条记录的题号 {seq} 不在这场练习里"}), 400
        if seq in answers:
            return jsonify({"error": f"第 {seq} 题提交了不止一次"}), 400

        answer = rec.get("user_answer")
        # 只收标量：dict/list 直接 str() 会存成 "{'a': 1}" 这样的脏答案显示在结果页上
        if answer is not None and not isinstance(answer, (str, int, float, bool)):
            return jsonify({"error": f"第 {index} 条记录的 user_answer 必须是文本或数字"}), 400
        answers[seq] = (answer, own_recording_path(rec.get("recording_path"), uid))

    # 近似音逐题判分时锁定过的作答以服务器为准；没锁定过的题，服务器从没把答案给出去，
    # 前端交上来的选项照收（比如逐题判分的请求在弱网下没发出去）。
    locked = {r["seq"]: r["user_answer"] for r in db.execute(
        "SELECT seq, user_answer FROM practice_answers WHERE session_id = ?", (session_id,))}

    parsed = []
    for seq in sorted(issued):
        question = issued[seq]
        answer, recording = answers.get(seq, (None, None))
        if seq in locked:
            answer = locked[seq]
        payload = {k: v for k, v in question.items() if k != "seq"}
        parsed.append((
            seq,
            json.dumps(payload, ensure_ascii=False),
            None if answer is None else str(answer)[:200],
            1 if grading.judge(question, answer) else 0,
            recording,
        ))

    # 交卷只能交一次：以前对已交卷的会话再提交一遍，会把整场成绩删掉重写，
    # 等于开着一个「交完再改」的口子。上面那次读只是为了早点给出友好提示，
    # 真正的闸门是这条带条件的 UPDATE —— 它在同一个事务里拿到写锁，
    # 两个并发的交卷请求只会有一个改到这一行，另一个拿到 rowcount 0 后回滚。
    claimed = db.execute(
        "UPDATE practice_sessions SET finished = 1 WHERE id = ? AND finished = 0",
        (session_id,),
    ).rowcount
    if not claimed:
        db.rollback()
        return already_finished(session_id)

    db.execute("DELETE FROM practice_records WHERE session_id = ?", (session_id,))
    correct = 0
    for seq, payload_json, answer, is_correct, recording in parsed:
        correct += is_correct
        db.execute(
            "INSERT INTO practice_records(session_id, seq, question_json, user_answer, "
            "is_correct, recording_path) VALUES(?,?,?,?,?,?)",
            (session_id, seq, payload_json, answer, is_correct, recording),
        )
    total = len(parsed)
    db.execute(
        "UPDATE practice_sessions SET total = ?, correct_count = ?, wrong_count = ? "
        "WHERE id = ?",
        (total, correct, total - correct, session_id),
    )
    db.commit()
    return jsonify({
        "total": total,
        "correct": correct,
        "wrong": total - correct,
        "result_url": url_for("result_page", session_id=session_id),
    })


@app.post("/api/recording")
@api_login_required
def api_upload_recording():
    """保存用户朗读录音，返回可回放的 URL。"""
    if is_local():
        # 本地模式的录音进的是 IndexedDB（见 scripts/local-store.js），
        # 不写服务器的 data/recordings/：既省磁盘，也保证「这台机子上没有别人的声音」。
        return local_mode_refuse("recording")
    file = request.files.get("audio")
    if not file or not file.filename:
        return jsonify({"error": "没有收到音频"}), 400

    ext = file.filename.rsplit(".", 1)[-1].lower()
    if ext not in RECORDING_EXTENSIONS:
        ext = "webm"

    rel_dir = recording_prefix()
    os.makedirs(recording_dir(), exist_ok=True)
    name = f"u{current_user()['id']}_{uuid.uuid4().hex[:12]}.{ext}"
    file.save(os.path.join(recording_dir(), name))
    return jsonify({"path": f"{rel_dir}/{name}", "url": url_for("recordings", filename=name)})


def settings_payload(user):
    """把一份档案（账号 dict 或 local_profile()）里那套练习默认值摊平成 JSON。

    /api/me 单独暴露这份值是为了「API 报的默认值」和「学生打开练习页看到的默认值」
    不可能对不上：两边都走 rhythm_defaults() / overlap_default() 同一套边界。
    """
    rhythm = rhythm_defaults(user)
    return {
        "default_count": user["default_count"],
        "time_limit": user["time_limit"],
        "voice": user["voice"],       # 历史列：近似音现在每题随机抽男女声
        # 看读译 / 听读写那四个走的是渲染练习页同一套函数（并且一样夹回边界）
        "round1_seconds": float(rhythm["round1"]["value"]),
        "round2_seconds": float(rhythm["round2"]["value"]),
        "round_gap_seconds": float(rhythm["gap"]["value"]),
        "overlap_record": overlap_default(user),
    }


@app.get("/api/me")
def api_me():
    # mode 摆在最前面：前端据此决定要不要把数据往服务器写。本地模式下这里永远是
    # logged_in=False，哪怕浏览器还留着切模式之前的登录 cookie——
    # 那种 cookie 现在是废的，不该让前端以为自己有个账号。
    if is_local():
        return jsonify({"logged_in": False, "mode": "local",
                        "username": None, "nickname": None,
                        "settings": settings_payload(local_profile())})
    user = current_user()
    if not user:
        return jsonify({"logged_in": False, "mode": "server"})
    return jsonify({
        "logged_in": True,
        "mode": "server",
        "username": user["username"],
        "nickname": user["nickname"],
        "settings": settings_payload(user),
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=config.DEBUG)
