# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

英语词汇发音练习平台：近似音区分、看读译、听读写三个练习模块。Flask + SQLite 后端，
前端是原生 HTML/CSS/JS + Bootstrap 5（CDN），录音用 MediaRecorder。代码注释、文档、提交说明都用中文。
需求以 `docs/design.md` 为准，生产部署见 `docs/deployment.md`（模板在 `deploy/*.example`）。

## 常用命令

```bash
pip install -r requirements.txt           # 只依赖 Flask，没有构建、没有 lint 配置
python scripts/init_db.py                 # 建表 + 种子词 + 扫描 static/audio；可重复执行
python scripts/init_db.py --reset         # 清空重来：要输入 RESET 确认，先备份库与录音目录再删表
python app.py                             # http://127.0.0.1:5000，FLASK_DEBUG=true 开调试
python scripts/serve_https.py             # 自签 HTTPS 起服务，手机录音必须走 https 或 localhost

REGISTER_MODE=open python app.py          # 本地开发绕过邀请码；或 python scripts/manage_invite.py add 发个码
APP_MODE=local python app.py              # 免登录模式，成绩与录音只存浏览器
```

测试用标准库 unittest，全部在 `tests/` 下：

```bash
python -m unittest discover -s tests -v                                        # 全部
python -m unittest tests.test_app_modes.TestLocalMode -v                       # 单个类
python -m unittest tests.test_app_modes.TestLocalMode.test_pages_open_without_any_session -v  # 单个用例
```

`tests/test_app_modes.py` 需要先有 `data/app.db`（没有就整体 SkipTest）；`tests/test_accounts_and_invites.py` 自己用 `init_db.SCHEMA` 在临时目录建空库，不依赖开发库，新测试优先照它的 `FreshDbTestCase` 写。它把库复制到临时目录，改 `db.DB_PATH` 指向副本，
所以不会写开发库。切模式靠直接赋值 `config.APP_MODE`，测解析时用 `importlib.reload(config)`。
测试只覆盖后端；IndexedDB 那一半要在真浏览器里按 `docs/deployment.md` 第 8.2 节手工验。

## 架构要点

**配置在 import 时读一次。** `config.py` 从环境变量读 `APP_MODE`、`REGISTER_MODE`、`DATABASE_PATH`、
`RECORDING_DIR` 等，改完必须重启进程。非法值一律退回更安全的一侧：`APP_MODE` 退回 `server`（要登录），
`REGISTER_MODE` 退回 `invite`。改这块时保持 fail-closed。代码里要读 `config.APP_MODE` 而不是缓存它，
测试依赖运行时改这个属性。

**两种运行模式（`APP_MODE=server|local`）。**
- 后端：`app.py` 的 `is_local()` 决定是否需要登录；`local` 模式下写数据的接口
  （`/api/practice/session`、`/api/practice/finish`、`/api/recording`）被 `local_mode_refuse` 挡掉（409），
  服务器一个字节都不写。两种模式都仍需要数据库，因为题库从那里出。
- 前端：模式由后端渲染到 `<body data-mode>`。`scripts/practice-store.js` 是**唯一**的分叉点，
  `server` 走 API，`local` 走 `scripts/local-store.js`（IndexedDB）。三个练习脚本
  （`vowel.js`、`tworounds.js`）不允许自己判断模式，新增练习也只能经 `PracticeStore`。
  `base.html` 里 `local-store.js` 必须在 `practice-store.js` 之前加载。
- 本地模式的 sessionId 也是整数（IndexedDB 自增键），所以结果页共用 `/result/<int:session_id>`，
  由 `result_local.html` + `local-result.js` 在浏览器里画。

**前端 JS 不在 static/ 下。** 脚本放在 `scripts/*.js`，由 `/js/<name>` 路由提供，只放行 `.js`。
同目录还有 Python 运维脚本，两者混放是有意的。

**数据层。** `db.py` 的 `connect()` 给每个连接打 WAL、`busy_timeout`、`foreign_keys` 等 pragma，
脚本和测试也走它；请求内用 `get_db()` 复用。路径以 `db.DB_PATH` 为唯一来源。
表结构全在 `scripts/init_db.py`：`CREATE TABLE IF NOT EXISTS` 不会给老库补列，
所以加列必须同时写对应的 `ALTER TABLE` 迁移（参照文件里已有的 `audio_f`/`audio_m`、users 设置列的做法）。

**模块分工。** `app.py` 是全部路由与 API；`services/auth.py` 管注册登录和用户设置；
`services/registration.py` 管邀请码闸门（码存数据库，发码/吊销不用重启）；
`services/practice.py` 负责抽题，包括近似音对立对随机、选项乱序、每题随机男声/女声。

**练习流程。** 练习页先建会话，逐题作答与录音，结束时统一提交 `/api/practice/finish`，跳转结果页回看。
服务器模式的成绩由后端判：出题接口（`/api/vowel/questions`、`/api/words`）带上 `session_id`，
把发出的题和标准答案快照进 `practice_sessions.questions_json`；交卷时只采信「题号、作答、录音」，
对错由 `services/grading.py` 重算，没交的题算错，没登记过题的会话拒绝交卷（409）。
前端那份判分只管即时反馈和本地模式的成绩，看读译的中文规则在 `tworounds.js` 的 `judgeChinese`
与 `grading.judge_chinese` 里各有一份，**必须一起改**，`tests/test_server_grading.py` 会用 node 对照两边。
看读译 / 听读写的三个时长（第一轮、第二轮、题间停顿）边界在 `config.py` 的 `ROUND1_SECONDS` 等常量里，
用户默认值存在 `users` 表对应列，`local` 模式存在 localStorage 的 `ep_settings`。

## 改代码前要知道的约束

- **注册校验必须排在密码哈希之前。** scrypt 在 1 核部署机上很贵，先哈希再拒会变成放大攻击口。
  理由写在 `services/registration.py` 开头；应用层故意不做按 IP 限速，限速放在 nginx。
- **音频必须在用户手势里解锁。** `Common.playUrl` 整页复用一个 `<audio>`，`unlockAudio()` 在点「开始练习」时调用；
  逐题 `new Audio()` 会在 iOS 上静音。麦克风同理，边播边录时用 `Recorder.prime()` 一次申请、整场复用，
  结束时 `releaseStream()`。iOS 不支持的编码由 `scripts/common.js` 回退为自编码 WAV。
- **音频文件命名是约定。** 近似音为 `n.m-word.f.wav` / `.m.wav`，背诵词为 `word.f.mp3` / `word.m.mp3`。
  导入脚本（`import_audio_package.py`、`import_quiz3000.py`）复制音频后，要重跑 `init_db.py` 才会登记进库。
- **不能提交的数据。** `data/*.db*`、`data/recordings/`、`_e2e/`、`_scratch/` 里有账号哈希与学生录音。
- 模板改动要重启开发服务器才生效（debug 默认关）。
