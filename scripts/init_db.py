# scripts/init_db.py
"""建表 + 灌入种子数据。

用法（在项目根目录）：
    python scripts/init_db.py                  # 幂等，重复执行不会重复插入
    python scripts/init_db.py --reset          # 先备份，再删掉已有表重建（要输入 RESET 确认）
    python scripts/init_db.py --reset --yes    # 非交互环境（脚本、CI）里跳过确认，备份照做
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time

# Windows 控制台默认 GBK，打印音标（/iː/ 之类）会抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# init_db.py 是「python scripts/init_db.py」这样直接跑的，sys.path[0] 是 scripts/，
# 所以要用根目录下的模块得先把根塞进去。
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from db import connect as connect_db, resolve_db_path, resolve_recording_dir  # noqa: E402
from services import registration  # noqa: E402  只为拿那两张邀请码表的 DDL，见下面 SCHEMA

# 库路径和 pragma 都从 db.py 拿，不要再在这里写死第二份 ——
# 以前两边各写一份 BASE_DIR/data/app.db，结果 config.DATABASE_PATH 形同虚设，
# 部署时改环境变量换不了库，还得连这个脚本一起改，很容易漏。
DB_PATH = resolve_db_path()
VOWEL_AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio", "vowel")
SENTENCE_AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio", "sentence")
WORD_AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio", "word")
# Quiz3000 课堂背诵词的男女声朗读，一个词两个文件，和近似音模块一样按性别分列存
WORD3000_AUDIO_DIR = os.path.join(BASE_DIR, "static", "audio", "word3000")
# 整句文本：由 scripts/import_audio_package.py 转写 4A 后写出，这里只负责登记进库
SENTENCE_TEXTS = os.path.join(BASE_DIR, "scripts", "sentence_texts.json")
# Quiz3000 词表（单词 + 音标 + 中文释义）：由 scripts/build_quiz3000_words.py 从
# ECDICT 词典生成后进版本库，音频目录本身不带任何文本信息
QUIZ3000_WORDS = os.path.join(BASE_DIR, "scripts", "quiz3000_words.json")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    nickname      TEXT,
    default_count INTEGER NOT NULL DEFAULT 20,   -- 默认练习题量
    time_limit    REAL    NOT NULL DEFAULT 1,    -- 近似音每题限时秒数
    voice         TEXT    NOT NULL DEFAULT 'f',  -- f=女声 m=男声（历史列，近似音已改为每题随机）
    -- 看读译 / 听读写那三段时长 + 边播边录开关，存进账号当默认值（/settings 里改）。
    -- 下面这几个 DEFAULT 只是「新注册的人没填过」时的初值，必须和 config.py 里
    -- ROUND1_SECONDS / ROUND2_SECONDS / ROUND_GAP_SECONDS 的第三个数保持一致，
    -- 真正的边界与夹取在 services/auth.update_settings 里做。
    round1_seconds    REAL    NOT NULL DEFAULT 1.5,  -- 第一轮（朗读 / 跟读录音）秒数
    round2_seconds    REAL    NOT NULL DEFAULT 5,    -- 第二轮（写译文 / 拼写）秒数
    round_gap_seconds REAL    NOT NULL DEFAULT 0.2,  -- 题间停顿秒数
    overlap_record    INTEGER NOT NULL DEFAULT 1,    -- 听读写第一轮边播边录
    created_at    TIMESTAMP DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS words (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    word       TEXT NOT NULL UNIQUE,
    phonetic   TEXT,
    meaning_cn TEXT,
    example    TEXT,
    audio_path TEXT,               -- 有真人音频时填相对路径，否则前端用合成发音
    audio_f    TEXT,               -- 女声朗读（Quiz3000），出题时和 audio_m 二选一随机
    audio_m    TEXT,               -- 男声朗读（Quiz3000）
    category   TEXT
);

CREATE TABLE IF NOT EXISTS vowel_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_key   TEXT NOT NULL UNIQUE,   -- 目录名，如 sheep-ship
    label       TEXT NOT NULL,          -- 如 /iː/ 与 /ɪ/
    description TEXT,
    pair_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vowel_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES vowel_groups(id) ON DELETE CASCADE,
    item_no    INTEGER NOT NULL,        -- 第几组最小对立对，如 3
    option_no  INTEGER NOT NULL,        -- 1 或 2，对应屏幕上的两个编号
    word       TEXT    NOT NULL,
    audio_f    TEXT    NOT NULL,
    audio_m    TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_vowel_item ON vowel_items(group_id, item_no, option_no);

-- 句子近似音（教材 4A）：同一组对立对换成整句朗读，屏幕上只给出句中变化的那两个词。
-- 整句文本由语音识别转写后存在 scripts/sentence_texts.json，这里登记进 sentence 字段。
-- 音频允许只有一种性别（4A 的 q / s 两个发音人不保证每课都齐），所以 audio_f / audio_m 可空。
CREATE TABLE IF NOT EXISTS sentence_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id   INTEGER NOT NULL REFERENCES vowel_groups(id) ON DELETE CASCADE,
    item_no    INTEGER NOT NULL,
    option_no  INTEGER NOT NULL,
    word       TEXT    NOT NULL,
    sentence   TEXT    NOT NULL,
    audio_f    TEXT,
    audio_m    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_sentence_item ON sentence_items(group_id, item_no, option_no);

CREATE TABLE IF NOT EXISTS practice_sessions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    practice_type TEXT    NOT NULL,     -- vowel / read-translate / listen-spell
    params_json   TEXT,
    total         INTEGER NOT NULL DEFAULT 0,
    correct_count INTEGER NOT NULL DEFAULT 0,
    wrong_count   INTEGER NOT NULL DEFAULT 0,
    finished      INTEGER NOT NULL DEFAULT 0,
    questions_json TEXT,                -- 出题时登记的题目与标准答案，交卷时据此在后端判分
    created_at    TIMESTAMP DEFAULT (datetime('now','localtime'))
);

-- 近似音逐题判分时锁定的作答：一题只认第一次提交的选项。
-- 服务器模式下出题接口不下发标准答案，学生作答时才经 /api/vowel/answer 换回对错和答案，
-- 所以必须在这一刻把作答定死，否则可以先拿 1 号试出答案、交卷时再改成对的那个。
CREATE TABLE IF NOT EXISTS practice_answers (
    session_id  INTEGER NOT NULL REFERENCES practice_sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    user_answer TEXT,                   -- 选项编号；超时为 NULL
    created_at  TIMESTAMP DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (session_id, seq)
);

CREATE TABLE IF NOT EXISTS practice_records (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES practice_sessions(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    question_json  TEXT    NOT NULL,    -- 该题的完整信息，供结果页逐题回看
    user_answer    TEXT,
    is_correct     INTEGER NOT NULL DEFAULT 0,
    recording_path TEXT,                -- 用户上传的朗读音频相对路径
    created_at     TIMESTAMP DEFAULT (datetime('now','localtime'))
);
"""

# 邀请码那两张表的定义放在 services/registration.py —— 闸门逻辑和表结构必须在
# 同一个文件里演进，写两份迟早会走偏。这里拼进来，init_db 一次建全。
SCHEMA += "\n".join(registration.TABLE_DDL)

DROP = """
DROP TABLE IF EXISTS invite_uses;
DROP TABLE IF EXISTS invite_codes;
DROP TABLE IF EXISTS practice_answers;
DROP TABLE IF EXISTS practice_records;
DROP TABLE IF EXISTS practice_sessions;
DROP TABLE IF EXISTS sentence_items;
DROP TABLE IF EXISTS vowel_items;
DROP TABLE IF EXISTS vowel_groups;
DROP TABLE IF EXISTS words;
DROP TABLE IF EXISTS users;
"""

# 近似音组目录名 → 音标对照说明。
# 目录来自教材配套录音的 3B（单词最小对立对），组名取该组一对代表词，
# 具体音频由 scripts/import_audio_package.py 复制，这里只负责显示说明。
VOWEL_GROUP_LABELS = {
    # 原有 4 组
    "see-say":     ("/iː/ 与 /eɪ/", "长元音 /iː/ 与双元音 /eɪ/ 的对立，如 see / say（教材第 1 课）"),
    "sheep-ship":  ("/iː/ 与 /ɪ/",  "长元音 /iː/ 与短元音 /ɪ/ 的对立，如 sheep / ship（教材第 2 课）"),
    "big-beg":     ("/ɪ/ 与 /e/",   "短元音 /ɪ/ 与 /e/ 的对立，如 big / beg（教材第 3 课）"),
    "pull-pool":   ("/ʊ/ 与 /uː/",  "短元音 /ʊ/ 与长元音 /uː/ 的对立，如 pull / pool（教材第 12 课）"),
    # 元音对立
    "pan-pen":     ("/æ/ 与 /e/",   "梅花音 /æ/ 与 /e/ 的对立，如 pan / pen、had / head（教材第 4 课）"),
    "cub-curb":    ("/ʌ/ 与 /ɜːr/", "短元音 /ʌ/ 与卷舌 /ɜːr/ 的对立，如 cub / curb、bud / bird（教材第 8 课）"),
    "fond-fund":   ("/ɒ/ 与 /ʌ/",   "圆唇 /ɒ/（美式读 /ɑː/）与 /ʌ/ 的对立，如 fond / fund、cop / cup（教材第 9 课）"),
    "boss-bus":    ("/ɒ/ 与 /ʌ/",   "同上组的另一组练习词，含 caught / cut 这类特殊拼读（教材第 10 课 a）"),
    "burn-barn":   ("/ɜːr/ 与 /ɑːr/", "卷舌 /ɜːr/ 与 /ɑːr/ 的对立，如 burn / barn、fur / far（教材第 10 课 c）"),
    "full-fall":   ("/ʊ/ 与 /ɔː/",  "短元音 /ʊ/ 与长元音 /ɔː/ 的对立，如 full / fall、took / talk（教材第 11 课）"),
    "pain-pen":    ("/eɪ/ 与 /e/",  "双元音 /eɪ/ 与单元音 /e/ 的对立，如 pain / pen、date / debt（教材第 13 课）"),
    "foal-fall":   ("/oʊ/ 与 /ɔː/", "双元音 /oʊ/ 与长元音 /ɔː/ 的对立，如 foal / fall、sew / saw（教材第 14 课）"),
    "kite-cat":    ("/aɪ/ 与 /æ/",  "双元音 /aɪ/ 与梅花音 /æ/ 的对立，如 kite / cat、hide / had（教材第 15 课）"),
    "boil-bawl":   ("/ɔɪ/ 与 /ɔː/", "双元音 /ɔɪ/ 与长元音 /ɔː/ 的对立，如 boil / bawl、poise / pause（教材第 16 课）"),
    "pound-pond":  ("/aʊ/ 与 /ɒ/",  "双元音 /aʊ/ 与短元音 /ɒ/ 的对立，如 pound / pond、down / Don（教材第 17 课）"),
    # 辅音与音素有无的对立
    "woos-ooze":   ("/w/ /j/ 与 无辅音", "词首半元音 /w/ /j/ 是否发音的对立，如 woos / ooze、year / ear（教材第 19 课）"),
    "row-low":     ("/r/ 与 /l/",   "舌侧音 /l/ 与 /r/ 的对立，如 row / low、read / lead（教材第 20 课）"),
    "world-word":  ("/l/ 与 无 /l/", "词尾 /l/ 是否发音的对立，如 world / word、cold / code、silk / sick（教材第 21 课）"),
    "no-low":      ("/n/ 与 /l/",   "鼻音 /n/ 与舌侧音 /l/ 的对立，如 no / low、need / lead（教材第 22 课）"),
    "comb-cone":   ("/m/ 与 /n/",   "词尾鼻音 /m/ 与 /n/ 的对立，如 comb / cone、gum / gun（教材第 23 课）"),
    "sing-sin":    ("/ŋ/ 与 /n/",   "舌后鼻音 /ŋ/ 与舌尖鼻音 /n/ 的对立，如 sing / sin、thing / thin（教材第 24 课）"),
    "heat-eat":    ("/h/ 与 无 /h/", "词首气流音 /h/ 是否发音的对立，如 heat / eat、hair / air（教材第 25 课）"),
    "feel-veal":   ("/f/ 与 /v/",   "清唇齿擦音 /f/ 与浊音 /v/ 的对立，如 feel / veal、fan / van（教材第 27 课）"),
    "seal-zeal":   ("/s/ 与 /z/",   "清擦音 /s/ 与浊擦音 /z/ 的对立，如 seal / zeal、race / raise（教材第 29 课）"),
    "fin-thin":    ("/f/ 与 /θ/",   "唇齿音 /f/ 与咬舌 /θ/ 的对立，如 fin / thin、free / three（教材第 30 课）"),
    "teethe-tease": ("/ð/ 与 /z/",  "咬舌浊音 /ð/ 与 /z/ 的对立，如 teethe / tease、scythe / size（教材第 31 课）"),
    "shave-save":  ("/ʃ/ 与 /s/",   "翘舌清擦音 /ʃ/ 与 /s/ 的对立，如 shave / save、sheet / seat（教材第 32 课）"),
    "pin-bin":     ("/p/ 与 /b/",   "清塞音 /p/ 与浊塞音 /b/ 的对立，如 pin / bin、cup / cub（教材第 35 课）"),
    "tin-din":     ("/t/ 与 /d/",   "清塞音 /t/ 与浊塞音 /d/ 的对立，如 tin / din、seat / seed（教材第 37 课）"),
    "cap-gap":     ("/k/ 与 /ɡ/",   "清塞音 /k/ 与浊塞音 /ɡ/ 的对立，如 cap / gap、coat / goat（教材第 39 课）"),
    "cheer-jeer":  ("/tʃ/ 与 /dʒ/", "破擦音 /tʃ/ 与 /dʒ/ 的对立，如 cheer / jeer、batch / badge（教材第 41 课）"),
}

# 音频文件名：n.m-word.f.wav / n.m-word.m.wav（第 22 课是 mp3，两种都要认）
AUDIO_NAME_RE = re.compile(r"^(\d+)\.(\d+)-(.+?)\.([fm])\.(wav|mp3)$", re.IGNORECASE)

# 单词音频文件名：<word>.wav / <word>.mp3，专有名词保留首字母大写（Paul、Tuesday）
WORD_AUDIO_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z'\-]*?)\.(wav|mp3)$", re.IGNORECASE)

# Quiz3000 的单词音频：<word>.f.mp3 / <word>.m.mp3。文件名里的词一律小写（Windows
# 文件系统不区分大小写，China / china 这种同名异义词会撞车），展示用的大小写看词表。
WORD3000_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9'\- ]*?)\.([fm])\.(wav|mp3)$", re.IGNORECASE)

SEED_WORDS = [
    ("abandon", "/əˈbændən/", "放弃；抛弃", "He abandoned his car in the snow."),
    ("ability", "/əˈbɪləti/", "能力；才能", "She has the ability to solve hard problems."),
    ("absent", "/ˈæbsənt/", "缺席的；缺少的", "He was absent from school yesterday."),
    ("achieve", "/əˈtʃiːv/", "实现；达到", "You can achieve your goal with effort."),
    ("active", "/ˈæktɪv/", "积极的；活跃的", "She is very active in class."),
    ("admire", "/ədˈmaɪər/", "钦佩；赞赏", "I admire your courage."),
    ("advance", "/ədˈvæns/", "前进；进步", "Please advance to the next room."),
    ("analysis", "/əˈnæləsɪs/", "分析", "The analysis took three weeks."),
    ("ancient", "/ˈeɪnʃənt/", "古代的；古老的", "We visited an ancient temple."),
    ("anxious", "/ˈæŋkʃəs/", "焦虑的；渴望的", "She felt anxious about the exam."),
    ("apparent", "/əˈpærənt/", "明显的；表面上的", "It was apparent that he was lying."),
    ("appreciate", "/əˈpriːʃieɪt/", "感激；欣赏", "I really appreciate your help."),
    ("approach", "/əˈproʊtʃ/", "方法；接近", "Try a different approach."),
    ("appropriate", "/əˈproʊpriət/", "适当的；恰当的", "Wear appropriate clothes."),
    ("arrange", "/əˈreɪndʒ/", "安排；整理", "Let me arrange a meeting."),
    ("attitude", "/ˈætɪtjuːd/", "态度；看法", "She has a positive attitude."),
    ("available", "/əˈveɪləbl/", "可用的；有空的", "Is this seat available?"),
    ("balance", "/ˈbæləns/", "平衡；余额", "Keep a balance between work and rest."),
    ("barrier", "/ˈbæriər/", "障碍；屏障", "Language is a barrier to communication."),
    ("behavior", "/bɪˈheɪvjər/", "行为；举止", "His behavior surprised everyone."),
    ("benefit", "/ˈbenɪfɪt/", "利益；好处", "Exercise has many benefits."),
    ("capacity", "/kəˈpæsəti/", "容量；能力", "The stadium has a capacity of 60,000."),
    ("challenge", "/ˈtʃælɪndʒ/", "挑战", "This job is a real challenge."),
    ("circumstance", "/ˈsɜːrkəmstæns/", "环境；情况", "Under no circumstances give up."),
    ("commit", "/kəˈmɪt/", "承诺；投入", "She committed to finishing the project."),
    ("community", "/kəˈmjuːnəti/", "社区；团体", "Our community is friendly."),
    ("compete", "/kəmˈpiːt/", "竞争；比赛", "Ten students competed for the prize."),
    ("complex", "/kəmˈpleks/", "复杂的", "This is a complex issue."),
    ("consider", "/kənˈsɪdər/", "考虑；思考", "Please consider my suggestion."),
    ("constant", "/ˈkɑːnstənt/", "持续的；恒定的", "The machine needs constant attention."),
]

# 教材配套录音各课 3A 的单词（真人音频放在 static/audio/word/，由 scan_word_audio 登记）。
# 单字母（R、Z、C、H）和不是单词的文件名（theam、tode）已经排除，不进词库。
SEED_WORDS_TEXTBOOK = [
    ("age", "/eɪdʒ/", "年龄；时期", "He started school at the age of five."),
    ("ash", "/æʃ/", "灰；灰烬", "The fire turned the paper into ash."),
    ("attempt", "/əˈtempt/", "尝试；努力", "She attempted to climb the wall."),
    ("back", "/bæk/", "背；后面；回来", "He leaned back in the chair."),
    ("badge", "/bædʒ/", "徽章；标记", "He wore a name badge at the meeting."),
    ("bag", "/bæɡ/", "包；袋子", "My bag is full of books."),
    ("ball", "/bɔːl/", "球；舞会", "The kids played ball in the park."),
    ("ban", "/bæn/", "禁止；禁令", "The city will ban cars in the center."),
    ("bang", "/bæŋ/", "砰地关上；巨响", "He banged the door shut."),
    ("bank", "/bæŋk/", "银行；河岸", "She works at a bank downtown."),
    ("barn", "/bɑːrn/", "谷仓；牲口棚", "The cows stayed in the barn."),
    ("bat", "/bæt/", "球棒；蝙蝠", "He swung the bat but missed."),
    ("beg", "/beɡ/", "乞求；恳求", "I beg your pardon."),
    ("bell", "/bel/", "铃；钟", "The bell rang at noon."),
    ("big", "/bɪɡ/", "大的", "They live in a big house."),
    ("bike", "/baɪk/", "自行车", "I ride my bike to school."),
    ("bill", "/bɪl/", "账单；纸币", "The hotel bill was very high."),
    ("bin", "/bɪn/", "垃圾箱；箱子", "Throw the box in the bin."),
    ("boil", "/bɔɪl/", "煮沸；沸腾", "Wait until the water boils."),
    ("bore", "/bɔːr/", "使厌烦；钻孔", "The long trip bored the children."),
    ("born", "/bɔːrn/", "出生的；天生的", "She was born in 2001."),
    ("borough", "/ˈbʌroʊ/", "行政区；自治市", "The borough has its own council."),
    ("boss", "/bɔːs/", "老板；上司", "My boss is on holiday this week."),
    ("bow", "/boʊ/", "弓；蝴蝶结（鞠躬读 /baʊ/）", "He shot an arrow with a bow."),
    ("bowl", "/boʊl/", "碗；钵", "Fill the dog's bowl with water."),
    ("breathe", "/briːð/", "呼吸", "Breathe slowly and relax."),
    ("breeze", "/briːz/", "微风", "A cool breeze blew through the window."),
    ("bud", "/bʌd/", "芽；花苞", "The rose bud opened in the sun."),
    ("bug", "/bʌɡ/", "虫子；故障", "A bug got into the tent at night."),
    ("bull", "/bʊl/", "公牛", "The bull ran across the field."),
    ("burrow", "/ˈbɜːroʊ/", "地洞；挖洞", "The rabbit dug a deep burrow."),
    ("bus", "/bʌs/", "公共汽车", "I take the bus to work every day."),
    ("call", "/kɔːl/", "打电话；呼叫", "Call me after dinner."),
    ("card", "/kɑːrd/", "卡片；纸牌", "She sent me a birthday card."),
    ("cat", "/kæt/", "猫", "The cat slept on the sofa."),
    ("caught", "/kɔːt/", "抓住（catch 的过去式）", "He caught the ball with one hand."),
    ("close", "/kloʊz/", "关闭；结束", "Please close the window."),
    ("clothe", "/kloʊð/", "给…穿衣；覆盖", "She clothed the baby warmly."),
    ("code", "/koʊd/", "代码；密码", "Enter your access code here."),
    ("coil", "/kɔɪl/", "线圈；盘绕", "Wind the wire into a tight coil."),
    ("cold", "/koʊld/", "寒冷的；感冒", "It is very cold outside."),
    ("colonel", "/ˈkɜːrnl/", "上校", "The colonel gave the order."),
    ("comb", "/koʊm/", "梳子；梳理", "He ran a comb through his hair."),
    ("cone", "/koʊn/", "圆锥体；蛋筒", "The kids ate ice cream from a cone."),
    ("cooed", "/kuːd/", "咕咕叫（coo 的过去式）", "The pigeon cooed softly."),
    ("cop", "/kɑːp/", "警察（口语）", "The cop stopped the speeding car."),
    ("cord", "/kɔːrd/", "绳子；电线", "Pull the cord to turn it on."),
    ("cot", "/kɑːt/", "婴儿床；简易床", "The baby slept in a wooden cot."),
    ("could", "/kʊd/", "能够（can 的过去式）", "We could hear the music next door."),
    ("courage", "/ˈkɜːrɪdʒ/", "勇气；胆量", "It took courage to speak in public."),
    ("cub", "/kʌb/", "幼兽；小熊", "A lion cub played with its tail."),
    ("cup", "/kʌp/", "杯子；奖杯", "Drink a cup of warm water."),
    ("current", "/ˈkɜːrənt/", "当前的；水流", "The current flows south in winter."),
    ("cut", "/kʌt/", "切；割", "He cut the paper with scissors."),
    ("dent", "/dent/", "凹痕；损伤", "There is a dent in the car door."),
    ("dime", "/daɪm/", "一角硬币", "A dime is worth ten cents."),
    ("din", "/dɪn/", "嘈杂声；喧闹", "There was a loud din in the hall."),
    ("dock", "/dɑːk/", "码头；入库", "The ship arrived at the dock."),
    ("dog", "/dɔːɡ/", "狗", "The dog barked all night."),
    ("Don", "/dɑːn/", "唐（男名）", "Don is my next-door neighbour."),
    ("down", "/daʊn/", "向下；下降", "He sat down slowly."),
    ("duck", "/dʌk/", "鸭子；躲避", "A duck swam across the pond."),
    ("due", "/duː/", "到期的；应有的", "The book is due next Monday."),
    ("dug", "/dʌɡ/", "挖（dig 的过去式）", "The dog dug a hole in the yard."),
    ("empty", "/ˈempti/", "空的；倒空", "The room was empty at midnight."),
    ("example", "/ɪɡˈzæmpl/", "例子；榜样", "Let me give you a simple example."),
    ("fail", "/feɪl/", "失败；不及格", "He failed the driving test."),
    ("fall", "/fɔːl/", "落下；秋天", "Leaves fall in autumn."),
    ("far", "/fɑːr/", "远的；遥远地", "The station is not far from here."),
    ("feel", "/fiːl/", "感觉；觉得", "I feel much better today."),
    ("few", "/fjuː/", "很少的；几乎没有", "Only a few people came to the talk."),
    ("field", "/fiːld/", "田地；领域", "They walked across the open field."),
    ("file", "/faɪl/", "文件；档案", "Save the file on the desktop."),
    ("fire", "/ˈfaɪər/", "火；解雇", "The fire spread quickly in the wind."),
    ("foal", "/foʊl/", "小马驹", "The foal followed its mother."),
    ("foe", "/foʊ/", "敌人；反对者", "He defeated an old foe."),
    ("fond", "/fɑːnd/", "喜爱的；溺爱的", "She is fond of classical music."),
    ("fool", "/fuːl/", "傻瓜；愚弄", "Don't be a fool."),
    ("found", "/faʊnd/", "建立；创办（find 的过去式为 found）", "She decided to found a chess club."),
    ("four", "/fɔːr/", "四", "A table has four legs."),
    ("full", "/fʊl/", "满的；完全的", "The glass is full of juice."),
    ("fund", "/fʌnd/", "基金；资助", "They set up a scholarship fund."),
    ("gap", "/ɡæp/", "缺口；差距", "There is a gap in the wall."),
    ("goat", "/ɡoʊt/", "山羊", "The goat ate some green leaves."),
    ("guard", "/ɡɑːrd/", "警卫；防护", "A guard stands at the gate."),
    ("gull", "/ɡʌl/", "海鸥", "A gull took the bread from my hand."),
    ("gum", "/ɡʌm/", "口香糖；牙龈", "She blew a bubble with gum."),
    ("gun", "/ɡʌn/", "枪", "The toy gun made a loud noise."),
    ("had", "/hæd/", "有（have 的过去式）", "We had a long meeting yesterday."),
    ("hair", "/her/", "头发；毛发", "His hair is short and black."),
    ("halve", "/hæv/", "对半分；减半", "We can halve the cost together."),
    ("happy", "/ˈhæpi/", "快乐的；高兴的", "She seems happy this morning."),
    ("hard", "/hɑːrd/", "困难的；硬的", "This question is too hard."),
    ("hate", "/heɪt/", "讨厌；憎恨", "I hate waiting in line."),
    ("head", "/hed/", "头；负责人", "He hit his head on the low door."),
    ("heart", "/hɑːrt/", "心脏；中心", "My heart beats fast when I run."),
    ("heat", "/hiːt/", "热；加热", "Heat the soup for two minutes."),
    ("heel", "/hiːl/", "脚后跟；鞋跟", "These shoes have high heels."),
    ("here", "/hɪr/", "这里", "Come here, please."),
    ("hide", "/haɪd/", "躲藏；隐藏", "The cat hid under the bed."),
    ("hill", "/hɪl/", "小山；丘陵", "They climbed the hill before dawn."),
    ("hoard", "/hɔːrd/", "贮存；积藏", "The squirrel hoarded nuts for winter."),
    ("home", "/hoʊm/", "家；国内的", "I went home early yesterday."),
    ("hour", "/ˈaʊər/", "小时；钟头", "The trip took one hour."),
    ("hurry", "/ˈhɜːri/", "匆忙；赶快", "Hurry up, we are late."),
    ("hurt", "/hɜːrt/", "疼痛；伤害", "My back hurts after sitting all day."),
    ("hut", "/hʌt/", "小屋；棚屋", "They slept in a stone hut."),
    ("Jane", "/dʒeɪn/", "简（女名）", "Jane lives at the end of the street."),
    ("jaw", "/dʒɔː/", "颌；下巴", "The lion opened its huge jaws."),
    ("jeer", "/dʒɪr/", "起哄；嘲笑", "The crowd began to jeer at him."),
    ("joke", "/dʒoʊk/", "笑话；开玩笑", "He told a funny joke."),
    ("joy", "/dʒɔɪ/", "欢乐；乐趣", "The baby brought us great joy."),
    ("kite", "/kaɪt/", "风筝", "The boy flew a kite in the field."),
    ("lack", "/læk/", "缺乏；缺少", "The plants lack water in summer."),
    ("lead", "/liːd/", "带领；主角（金属铅读 /led/）", "She will lead the research team."),
    ("leave", "/liːv/", "离开；留下", "The train leaves at six."),
    ("led", "/led/", "带领（lead 的过去式）", "He led us through the forest."),
    ("lid", "/lɪd/", "盖子", "Lift the lid carefully."),
    ("light", "/laɪt/", "光；灯；轻的", "Turn on the light, it is dark."),
    ("like", "/laɪk/", "喜欢；像", "I like this song very much."),
    ("live", "/lɪv/", "居住；活着的", "They live in a small flat."),
    ("load", "/loʊd/", "装载；负担", "Load the boxes into the truck."),
    ("long", "/lɔːŋ/", "长的；长久的", "The meeting was very long."),
    ("look", "/lʊk/", "看；表情", "Look at this picture."),
    ("lost", "/lɔːst/", "丢失的；迷路的", "I lost my keys this morning."),
    ("lot", "/lɑːt/", "许多；场地", "Thanks a lot for your help."),
    ("low", "/loʊ/", "低的；浅的", "The plane flew low over the river."),
    ("luck", "/lʌk/", "运气；幸运", "Good luck with your exam."),
    ("Luke", "/luːk/", "卢克（男名）", "Luke is a careful driver."),
    ("lung", "/lʌŋ/", "肺", "He hurt his lung in the accident."),
    ("lust", "/lʌst/", "强烈欲望；贪欲", "He has little lust for money."),
    ("mat", "/mæt/", "垫子；席子", "Wipe your feet on the mat."),
    ("mate", "/meɪt/", "伙伴；配偶", "He went fishing with his mate."),
    ("member", "/ˈmembər/", "成员；会员", "She is a member of the club."),
    ("mere", "/mɪr/", "仅仅的；只不过", "It is a mere three dollars."),
    ("mesh", "/meʃ/", "网眼；网状物", "The net has a fine mesh."),
    ("met", "/met/", "遇见（meet 的过去式）", "We met last summer."),
    ("mill", "/mɪl/", "磨坊；工厂", "The old mill is now a museum."),
    ("mode", "/moʊd/", "方式；模式", "The camera is in auto mode."),
    ("need", "/niːd/", "需要；必须", "I need your help with this."),
    ("new", "/nuː/", "新的；新鲜的", "They moved into a new house."),
    ("night", "/naɪt/", "夜晚；夜间", "The city is bright at night."),
    ("no", "/noʊ/", "不；没有", "She said no politely."),
    ("node", "/noʊd/", "节点；结", "Click the node to see the details."),
    ("not", "/nɑːt/", "不；不是", "He is not at home right now."),
    ("pain", "/peɪn/", "疼痛；痛苦", "I felt a sharp pain in my knee."),
    ("pan", "/pæn/", "平底锅", "Put some oil in the pan."),
    ("Paul", "/pɔːl/", "保罗（男名）", "Paul can play the piano well."),
    ("pause", "/pɔːz/", "暂停；停顿", "He paused for a moment to think."),
    ("peas", "/piːz/", "豌豆", "The children shelled the fresh peas."),
    ("pen", "/pen/", "钢笔；围栏", "Write it with a blue pen."),
    ("pet", "/pet/", "宠物；爱抚", "A pet dog needs a daily walk."),
    ("pit", "/pɪt/", "坑；果核", "The car fell into a deep pit."),
    ("poise", "/pɔɪz/", "镇定；保持平衡", "The bird poised on a thin branch."),
    ("pole", "/poʊl/", "杆；柱子；极", "He leaned on his long pole."),
    ("pond", "/pɑːnd/", "池塘", "Ducks swim in the small pond."),
    ("pool", "/puːl/", "水池；游泳池", "They swam in the hotel pool."),
    ("pound", "/paʊnd/", "磅；英镑；猛击", "He pounded the table with his fist."),
    ("pull", "/pʊl/", "拉；拔", "Pull the door to open it."),
    ("raider", "/ˈreɪdər/", "突袭者；劫匪", "The raiders took the gold and ran."),
    ("raise", "/reɪz/", "提高；举起；筹集", "They hope to raise more money."),
    ("ran", "/ræn/", "跑（run 的过去式）", "She ran to the station."),
    ("rang", "/ræŋ/", "鸣；响（ring 的过去式）", "The telephone rang twice."),
    ("rank", "/ræŋk/", "等级；排；把…排列", "The soldiers stood in rank."),
    ("rat", "/ræt/", "老鼠", "A big rat ran across the floor."),
    ("read", "/riːd/", "阅读（过去式同形，读 /red/）", "I read before bed every night."),
    ("reader", "/ˈriːdər/", "读者；读物", "He is a slow but careful reader."),
    ("right", "/raɪt/", "右边；正确的", "Turn right at the corner."),
    ("ring", "/rɪŋ/", "戒指；铃声；打电话", "She put on a gold ring."),
    ("rink", "/rɪŋk/", "溜冰场", "They skated at the ice rink."),
    ("rise", "/raɪz/", "上升；起身", "The sun rises in the east."),
    ("road", "/roʊd/", "路；道路", "This road leads to the port."),
    ("rob", "/rɑːb/", "抢劫；偷窃", "Two men tried to rob the bank."),
    ("robe", "/roʊb/", "长袍；礼服", "She wore a soft silk robe."),
    ("rot", "/rɑːt/", "腐烂；腐败", "The fruit began to rot in the heat."),
    ("row", "/roʊ/", "一排；划船（争吵读 /raʊ/）", "We row across the lake every Sunday."),
    ("rub", "/rʌb/", "擦；摩擦", "He rubbed his tired eyes."),
    ("rum", "/rʌm/", "朗姆酒", "They mixed the rum with fruit juice."),
    ("run", "/rʌn/", "跑；运转", "She runs in the park each morning."),
    ("Ruth", "/ruːθ/", "露丝（女名）", "Ruth sings in the church choir."),
    ("sail", "/seɪl/", "帆；航行", "The boat set sail at dawn."),
    ("sat", "/sæt/", "坐（sit 的过去式）", "He sat beside me on the bus."),
    ("saw", "/sɔː/", "看见（see 的过去式）；锯", "She saw a strange light at sea."),
    ("say", "/seɪ/", "说；讲", "What did you say just now?"),
    ("scythe", "/saɪθ/", "大镰刀", "The farmer cut the grass with a scythe."),
    ("seat", "/siːt/", "座位；使坐下", "Please take your seat."),
    ("see", "/siː/", "看见；明白", "I can see the mountain from here."),
    ("seed", "/siːd/", "种子；播种", "Plant the seed in wet soil."),
    ("sell", "/sel/", "卖；销售", "They sell fresh bread here."),
    ("sew", "/soʊ/", "缝；缝制", "She can sew her own clothes."),
    ("shade", "/ʃeɪd/", "阴凉；阴影", "The old tree gives cool shade."),
    ("shave", "/ʃeɪv/", "刮胡子；剃", "He shaves every morning."),
    ("shed", "/ʃed/", "棚屋；流出；脱落", "He stored his tools in the shed."),
    ("sheep", "/ʃiːp/", "绵羊", "The sheep grazed on the hill."),
    ("sheet", "/ʃiːt/", "床单；一张（纸）", "Change the bed sheet this week."),
    ("shin", "/ʃɪn/", "小腿胫", "He hurt his shin on the table."),
    ("ship", "/ʃɪp/", "轮船；运送", "A large ship left the port."),
    ("shot", "/ʃɑːt/", "射击（shoot 的过去式）；投篮", "The player took a long shot."),
    ("shout", "/ʃaʊt/", "喊；大叫", "Don't shout at the children."),
    ("shut", "/ʃʌt/", "关上；闭上", "Shut the window, please."),
    ("silk", "/sɪlk/", "丝绸；蚕丝", "The dress is made of pure silk."),
    ("simple", "/ˈsɪmpl/", "简单的；朴素的", "It was a simple question."),
    ("sin", "/sɪn/", "罪；犯罪", "He regretted his sin at once."),
    ("sing", "/sɪŋ/", "唱歌；歌唱", "She sings very beautifully."),
    ("sink", "/sɪŋk/", "水槽；下沉", "The dishes are still in the sink."),
    ("sit", "/sɪt/", "坐；就位", "Sit down and have a rest."),
    ("size", "/saɪz/", "大小；尺码", "What size do you wear?"),
    ("sleep", "/sliːp/", "睡觉；睡眠", "I slept eight hours last night."),
    ("slip", "/slɪp/", "滑倒；便条", "He slipped on the wet ice."),
    ("spell", "/spel/", "拼写；咒语", "Can you spell your family name?"),
    ("spill", "/spɪl/", "洒出；溢出", "He spilled the milk on the table."),
    ("star", "/stɑːr/", "星；明星", "The stars shine clearly tonight."),
    ("store", "/stɔːr/", "商店；贮存", "The store opens at nine."),
    ("sue", "/suː/", "起诉；告发", "They sued the company for damages."),
    ("sum", "/sʌm/", "总额；算术", "Add the numbers to get the sum."),
    ("sun", "/sʌn/", "太阳；阳光", "The sun sets in the west."),
    ("talk", "/tɔːk/", "谈话；讲话", "They talked for an hour."),
    ("tall", "/tɔːl/", "高的", "Her brother is very tall."),
    ("taste", "/teɪst/", "品尝；味道", "Taste the soup and add some salt."),
    ("tease", "/tiːz/", "取笑；戏弄", "The older boys tease their sister."),
    ("teethe", "/tiːð/", "出牙；长牙", "Babies usually teethe around six months."),
    ("test", "/test/", "测试；检验", "The English test was easy."),
    ("thin", "/θɪn/", "瘦的；薄的", "This paper is very thin."),
    ("thing", "/θɪŋ/", "东西；事情", "What is that black thing?"),
    ("think", "/θɪŋk/", "想；认为", "I think you are right."),
    ("thirst", "/θɜːrst/", "渴；渴望", "He drank water to quench his thirst."),
    ("thorough", "/ˈθɜːroʊ/", "彻底的；周密的", "She gave a thorough explanation."),
    ("three", "/θriː/", "三", "Three boys are playing outside."),
    ("toil", "/tɔɪl/", "辛苦劳作；苦役", "He toiled in the fields all day."),
    ("took", "/tʊk/", "拿；带走（take 的过去式）", "He reached over and took the cup."),
    ("towel", "/ˈtaʊəl/", "毛巾", "Dry your hands with a clean towel."),
    ("tower", "/ˈtaʊər/", "塔；高楼", "The old tower is still standing."),
    ("truth", "/truːθ/", "真相；事实", "Please tell me the truth."),
    ("Tuesday", "/ˈtuːzdeɪ/", "星期二", "We meet every Tuesday afternoon."),
    ("van", "/væn/", "厢式货车", "A white van pulled up outside."),
    ("veal", "/viːl/", "小牛肉", "He ordered roasted veal with rice."),
    ("veil", "/veɪl/", "面纱；遮蔽物", "The bride wore a white veil."),
    ("verse", "/vɜːrs/", "诗节；韵文", "She wrote the last verse of the song."),
    ("very", "/ˈveri/", "非常；正是", "It is very hot today."),
    ("vest", "/vest/", "背心；马甲", "He wore a warm wool vest."),
    ("vine", "/vaɪn/", "藤；葡萄藤", "Grapes grow on this vine."),
    ("wake", "/weɪk/", "醒来；唤醒", "I wake up at seven every day."),
    ("wall", "/wɔːl/", "墙；围墙", "A picture hung on the wall."),
    ("water", "/ˈwɔːtər/", "水；浇水", "Drink more water after running."),
    ("week", "/wiːk/", "周；星期", "See you next week."),
    ("whale", "/hweɪl/", "鲸", "A huge whale swam past the boat."),
    ("wheel", "/wiːl/", "轮子；方向盘", "The front wheel is bent."),
    ("when", "/wen/", "什么时候；当…时", "When do you leave for Beijing?"),
    ("whim", "/wɪm/", "突发奇想；一时兴起", "He bought the hat on a whim."),
    ("why", "/hwaɪ/", "为什么", "Why are you late again?"),
    ("win", "/wɪn/", "赢；获得", "Our team hopes to win."),
    ("wine", "/waɪn/", "葡萄酒；果酒", "He poured a glass of red wine."),
    ("won", "/wʌn/", "赢（win 的过去式）", "She won the race easily."),
    ("wood", "/wʊd/", "木头；树林", "The table is made of wood."),
    ("wooed", "/wuːd/", "求爱；争取（woo 的过去式）", "He wooed her with patience."),
    ("wool", "/wʊl/", "羊毛；毛线", "This sweater is pure wool."),
    ("woos", "/wuːz/", "求爱；争取（woo 的第三人称）", "He woos her with quiet words."),
    ("world", "/wɜːrld/", "世界", "She wants to travel the world."),
    ("worry", "/ˈwɜːri/", "担心；烦恼", "Don't worry about the exam."),
    ("worse", "/wɜːrs/", "更差的；更糟的", "The weather got worse at night."),
    ("worst", "/wɜːrst/", "最差的；最糟的", "That was my worst day so far."),
    ("worth", "/wɜːrθ/", "值得…的；价值", "Is the film worth the ticket price?"),
    ("wreath", "/riːθ/", "花环；花圈", "They laid a wreath at the memorial."),
    ("writhe", "/rɪð/", "扭动；翻滚", "The worm writhe on the hot stone."),
    ("yam", "/jæm/", "山药；红薯", "They roasted a yam over the fire."),
    ("yard", "/jɑːrd/", "院子；码", "The dog runs in the back yard."),
    ("yawn", "/jɔːn/", "打哈欠", "She yawned because she was tired."),
    ("year", "/jɪr/", "年；年度", "I stayed there a whole year."),
    ("yeast", "/jiːst/", "酵母", "Add some yeast to the dough."),
    ("yellow", "/ˈjeloʊ/", "黄色；黄色的", "The sun looks yellow at dusk."),
    ("yes", "/jes/", "是；是的", "She answered yes without thinking."),
    ("yoke", "/joʊk/", "牛轭；束缚", "The ox wore a wooden yoke."),
    ("young", "/jʌŋ/", "年轻的；幼小的", "The young man asked a good question."),
    ("youth", "/juːθ/", "青春；年轻人", "He spent his youth in the countryside."),
    ("zeal", "/ziːl/", "热情；热忱", "She works with great zeal."),
    ("zip", "/zɪp/", "拉链", "Pull up the zip of your jacket."),
]


def scan_vowel_audio(conn):
    """扫描 static/audio/vowel，把最小对立对音频写进 vowel_groups / vowel_items。"""
    if not os.path.isdir(VOWEL_AUDIO_DIR):
        print("未找到近似音音频目录，跳过：", VOWEL_AUDIO_DIR)
        return 0

    group_count = 0
    for group_key in sorted(os.listdir(VOWEL_AUDIO_DIR)):
        subdir = os.path.join(VOWEL_AUDIO_DIR, group_key)
        if not os.path.isdir(subdir):
            continue

        # 先按 (item_no, option_no) 收集，把同一题的男女声合并成一行
        items = {}
        for fname in sorted(os.listdir(subdir)):
            match = AUDIO_NAME_RE.match(fname)
            if not match:
                print("  跳过无法解析的文件名：", group_key, "/", fname)
                continue
            item_no, option_no, word, voice = match.group(1), match.group(2), match.group(3), match.group(4)
            rel = f"static/audio/vowel/{group_key}/{fname}"
            key = (int(item_no), int(option_no))
            row = items.setdefault(key, {"word": word, "f": None, "m": None})
            row[voice] = rel

        label, desc = VOWEL_GROUP_LABELS.get(group_key, (group_key, ""))
        conn.execute(
            "INSERT INTO vowel_groups(group_key, label, description) VALUES(?,?,?) "
            "ON CONFLICT(group_key) DO UPDATE SET label=excluded.label, description=excluded.description",
            (group_key, label, desc),
        )
        group_id = conn.execute(
            "SELECT id FROM vowel_groups WHERE group_key=?", (group_key,)
        ).fetchone()["id"]

        inserted = 0
        for (item_no, option_no), row in sorted(items.items()):
            if not (row["f"] and row["m"]):
                print(f"  缺少男声或女声，跳过 {group_key} 第{item_no}组选项{option_no}")
                continue
            conn.execute(
                "INSERT INTO vowel_items(group_id, item_no, option_no, word, audio_f, audio_m) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(group_id, item_no, option_no) DO UPDATE SET "
                "word=excluded.word, audio_f=excluded.audio_f, audio_m=excluded.audio_m",
                (group_id, item_no, option_no, row["word"], row["f"], row["m"]),
            )
            inserted += 1

        pair_count = len({no for (no, _) in items})
        conn.execute("UPDATE vowel_groups SET pair_count=? WHERE id=?", (pair_count, group_id))
        print(f"  {group_key:<12} {label:<12} 对立对 {pair_count} 组，音频 {inserted} 条")
        group_count += 1

    return group_count


def load_sentence_texts():
    """读 scripts/sentence_texts.json：{组名: {"对立对.选项": 整句}}。"""
    if not os.path.isfile(SENTENCE_TEXTS):
        return {}
    with open(SENTENCE_TEXTS, encoding="utf-8") as fh:
        return json.load(fh)


def scan_sentence_audio(conn):
    """扫描 static/audio/sentence，把 4A 的句子音频登记进 sentence_items。

    文件名沿用 3B 的规则（<对立对>.<选项>-<词>.<f|m>.mp3），所以每个 (对立对, 选项)
    必须能在 vowel_items 里找到对应的单词——句子练的就是那对词。整句文本从
    scripts/sentence_texts.json 取，取不到的不入库（结果页会显示空句，不如没有）。
    """
    if not os.path.isdir(SENTENCE_AUDIO_DIR):
        return 0, 0

    texts = load_sentence_texts()
    registered = missing_text = orphan = 0
    for group_key in sorted(os.listdir(SENTENCE_AUDIO_DIR)):
        subdir = os.path.join(SENTENCE_AUDIO_DIR, group_key)
        if not os.path.isdir(subdir):
            continue
        group_id = conn.execute(
            "SELECT id FROM vowel_groups WHERE group_key=?", (group_key,)
        ).fetchone()
        if not group_id:
            print(f"  句子目录 {group_key} 在近似音组里找不到，先跑 3B 的导入")
            orphan += 1
            continue
        gid = group_id["id"]
        group_texts = texts.get(group_key, {})
        # 整组先清空再按磁盘上的文件重建：删掉的音频不会在库里留下僵尸行
        conn.execute("DELETE FROM sentence_items WHERE group_id=?", (gid,))

        items = {}
        for fname in sorted(os.listdir(subdir)):
            match = AUDIO_NAME_RE.match(fname)
            if not match:
                print("  跳过无法解析的句子文件名：", group_key, "/", fname)
                continue
            item_no, option_no, word, voice = (
                int(match.group(1)), int(match.group(2)), match.group(3), match.group(4))
            key = (item_no, option_no)
            row = items.setdefault(key, {"word": word, "f": None, "m": None})
            row[voice] = f"static/audio/sentence/{group_key}/{fname}"

        for (item_no, option_no), row in sorted(items.items()):
            sentence = group_texts.get(f"{item_no}.{option_no}")
            word_row = conn.execute(
                "SELECT word FROM vowel_items WHERE group_id=? AND item_no=? AND option_no=?",
                (gid, item_no, option_no)).fetchone()
            if not sentence or not word_row:
                print(f"  {group_key} 第{item_no}组选项{option_no} 缺少"
                      f"{'整句文本' if not sentence else '对应的单词题'}，跳过")
                missing_text += 1
                continue
            if word_row["word"].lower() != row["word"].lower():
                print(f"  {group_key} 第{item_no}组选项{option_no} 文件名里的词 {row['word']} "
                      f"和单词题库 {word_row['word']} 不一致，跳过")
                orphan += 1
                continue
            conn.execute(
                "INSERT INTO sentence_items(group_id, item_no, option_no, word, sentence, "
                "audio_f, audio_m) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(group_id, item_no, option_no) DO UPDATE SET "
                "word=excluded.word, sentence=excluded.sentence, "
                "audio_f=excluded.audio_f, audio_m=excluded.audio_m",
                (gid, item_no, option_no, word_row["word"], sentence, row["f"], row["m"]),
            )
            registered += 1

    return registered, missing_text


def seed_words(conn):
    seed = SEED_WORDS + SEED_WORDS_TEXTBOOK
    for word, phonetic, cn, example in seed:
        conn.execute(
            "INSERT INTO words(word, phonetic, meaning_cn, example, category) "
            "VALUES(?,?,?,?,'vocabulary') ON CONFLICT(word) DO UPDATE SET "
            "phonetic=excluded.phonetic, meaning_cn=excluded.meaning_cn, example=excluded.example",
            (word, phonetic, cn, example),
        )
    return len(seed)


def scan_word_audio(conn):
    """扫描 static/audio/word，把教材 3A 的单词音频登记到 words.audio_path。

    只登记词库里已经存在的单词：没有释义的词进听读写会没声音、进看读译会没答案，
    所以文件名对不上词库的一律列为孤儿并打印出来，提醒先补 SEED_WORDS_TEXTBOOK。
    """
    if not os.path.isdir(WORD_AUDIO_DIR):
        print("未找到单词音频目录，跳过：", WORD_AUDIO_DIR)
        return 0, []

    matched, orphans, skipped = 0, [], []
    for fname in sorted(os.listdir(WORD_AUDIO_DIR)):
        if not os.path.isfile(os.path.join(WORD_AUDIO_DIR, fname)):
            continue
        match = WORD_AUDIO_NAME_RE.match(fname)
        if not match:
            skipped.append(fname)
            continue
        word = match.group(1)
        if not conn.execute("SELECT 1 FROM words WHERE word = ?", (word,)).fetchone():
            orphans.append(word)
            continue
        rel = f"static/audio/word/{fname}"
        conn.execute("UPDATE words SET audio_path = ? WHERE word = ?", (rel, word))
        matched += 1

    for fname in skipped:
        print("  跳过无法解析的文件名：", fname)
    return matched, orphans


def load_quiz3000_words():
    """读 scripts/quiz3000_words.json → {小写词形: [词表条目]}。

    同一个文件名可能对应两个词条（China / china 在 Windows 上只能存一个 mp3），
    所以按小写词形收集成列表，登记时逐个处理。
    """
    if not os.path.isfile(QUIZ3000_WORDS):
        return {}
    with open(QUIZ3000_WORDS, encoding="utf-8") as fh:
        entries = json.load(fh)["words"]
    by_key = {}
    for entry in entries:
        by_key.setdefault(entry["word"].lower(), []).append(entry)
    return by_key


def scan_quiz3000_audio(conn):
    """扫描 static/audio/word3000，把 Quiz3000 的男女声朗读登记进 words.audio_f / audio_m。

    只认词表里有的词形：词表是 scripts/build_quiz3000_words.py 用 ECDICT 词典筛过的，
    文件名对不上词表的一律算非单词（提示音、拼写错误之类），列为孤儿打印出来。
    词库里已有的 3A 种子词只补两个性别的音频，释义和例句保持手写的那份；
    上次从词表插入的词连音释义一起刷新；新词按词表插入，没有例句。
    词形按原大小写精确匹配，因为 China 和 china 是两道题。
    """
    if not os.path.isdir(WORD3000_AUDIO_DIR):
        return 0, 0, 0, []
    by_key = load_quiz3000_words()
    if not by_key:
        print("  词表 scripts/quiz3000_words.json 不在，跳过 Quiz3000 音频（先跑 "
              "scripts/build_quiz3000_words.py）")
        return 0, 0, 0, []

    on_disk = {}
    skipped = []
    for fname in sorted(os.listdir(WORD3000_AUDIO_DIR)):
        if not os.path.isfile(os.path.join(WORD3000_AUDIO_DIR, fname)):
            continue
        match = WORD3000_NAME_RE.match(fname)
        if not match:
            skipped.append(fname)
            continue
        key, voice = match.group(1).lower(), match.group(2).lower()
        on_disk.setdefault(key, {})[voice] = f"static/audio/word3000/{fname}"

    inserted = updated = seeded = 0
    orphans = []
    for key in sorted(on_disk):
        entries = by_key.get(key)
        if not entries:
            orphans.append(key)
            continue
        voices = on_disk[key]
        for entry in entries:
            word = entry["word"]
            row = conn.execute("SELECT id, category FROM words WHERE word = ?", (word,)).fetchone()
            params = (voices.get("f"), voices.get("m"))
            if row is None:
                conn.execute(
                    "INSERT INTO words(word, phonetic, meaning_cn, audio_f, audio_m, category) "
                    "VALUES(?,?,?,?,?,'quiz3000')",
                    (word, entry.get("phonetic"), entry.get("meaning_cn"),) + params,
                )
                inserted += 1
            elif row["category"] == "quiz3000":
                # 这批词的释义和音标只认词表：重新生成词表（纠正词典错译、统一音标字符）
                # 之后必须能跟着刷新，否则库里留的还是上一版词典原文
                conn.execute(
                    "UPDATE words SET audio_f = ?, audio_m = ?, phonetic = ?, meaning_cn = ? "
                    "WHERE id = ?",
                    params + (entry.get("phonetic"), entry.get("meaning_cn"), row["id"]),
                )
                updated += 1
            else:
                conn.execute("UPDATE words SET audio_f = ?, audio_m = ? WHERE id = ?",
                             params + (row["id"],))
                seeded += 1

    for fname in skipped:
        print("  跳过无法解析的文件名：", fname)
    return inserted, updated, seeded, orphans


def migrate_words_voice_columns(conn):
    """老库的 words 表没有 audio_f / audio_m，CREATE TABLE IF NOT EXISTS 不会补列，只能 ALTER。"""
    # 按列号取，理由同 migrate_users_rhythm_columns：拿裸连接（没设 row_factory）调用也不能崩
    columns = {row[1] for row in conn.execute("PRAGMA table_info(words)")}
    for name in ("audio_f", "audio_m"):
        if name not in columns:
            conn.execute("ALTER TABLE words ADD COLUMN %s TEXT" % name)
            print("已为 words 表补上 %s 列" % name)


# users 表后来加的「练习默认参数」列：列名 → ALTER 时给的初值。
# CREATE TABLE IF NOT EXISTS 对已经存在的表一个字段都不补，所以线上那个库
# （2026-11-17 就有用户了）必须靠这里补，否则 app.rhythm_defaults() 读不到列、
# 设置页一保存就报 no such column。初值和 config.py 里的默认值一致，
# 也就是「补完列之后老用户的体验和以前完全一样」——除了第一轮从 1 秒变成 1.5 秒，
# 那是这次有意改的。
USER_RHYTHM_COLUMNS = (
    ("round1_seconds", "REAL NOT NULL DEFAULT 1.5"),
    ("round2_seconds", "REAL NOT NULL DEFAULT 5"),
    ("round_gap_seconds", "REAL NOT NULL DEFAULT 0.2"),
    ("overlap_record", "INTEGER NOT NULL DEFAULT 1"),
)


def migrate_users_rhythm_columns(conn):
    """给老库的 users 表补上四个可保存的练习默认值列。"""
    # 按列号取而不是按列名（row["name"]）：这个函数不该要求调用方先设
    # conn.row_factory = sqlite3.Row —— init_db() 里设了，别处（比如校验脚本、
    # 或者将来把它接到部署流程里）拿裸连接一调就会 TypeError: tuple indices ...。
    # PRAGMA table_info 的第二列永远是列名，两种 row_factory 都成立。
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for name, ddl in USER_RHYTHM_COLUMNS:
        if name not in columns:
            conn.execute("ALTER TABLE users ADD COLUMN %s %s" % (name, ddl))
            print("已为 users 表补上 %s 列" % name)


def migrate_sessions_questions_column(conn):
    """给老库的 practice_sessions 补上 questions_json（后端判分用的出题快照）。

    没有这一列时，出题和交卷接口都会报 no such column。补列后老会话这一列是 NULL，
    它们早已交卷，不受影响；还没交卷的老会话交卷时会被要求刷新页面重新开始。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(practice_sessions)")}
    if "questions_json" not in columns:
        conn.execute("ALTER TABLE practice_sessions ADD COLUMN questions_json TEXT")
        print("已为 practice_sessions 表补上 questions_json 列")


def backup_before_reset(conn):
    """--reset 之前把库和录音目录都留一份，返回 (库备份路径, 录音挪到的路径)，没有的项为 None。

    库用 SQLite 的 backup API 拷：WAL 模式下直接复制 app.db 会漏掉 -wal 里还没合并的事务。
    录音目录整体改名而不是删除：一是和库备份成对，真要回滚两样都在；二是 reset 后
    sqlite_sequence 清零、用户 id 从 1 重新编号，而录音的归属看的是文件名里的 u<id>_，
    旧文件留在原处，新的 1 号用户就会在校验上「拥有」旧 1 号用户的录音。
    """
    stamp = time.strftime("%Y%m%d-%H%M%S")
    db_backup = None
    has_tables = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0]
    if has_tables:
        db_backup = "%s.bak-%s" % (DB_PATH, stamp)   # .gitignore 已忽略 data/*.db.bak-*
        dest = sqlite3.connect(db_backup)
        try:
            conn.backup(dest)
        finally:
            dest.close()

    rec_dir = resolve_recording_dir()
    rec_moved = None
    if os.path.isdir(rec_dir) and os.listdir(rec_dir):
        rec_moved = "%s.bak-%s" % (rec_dir.rstrip("/\\"), stamp)
        os.rename(rec_dir, rec_moved)
    return db_backup, rec_moved


def confirm_reset(assume_yes):
    """--reset 会删掉全部账号、成绩和邀请码，必须明确确认。"""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        print("--reset 需要确认，但当前不是交互终端；确实要清空请加 --yes。", file=sys.stderr)
        return False
    print("即将清空 %s 里的全部账号、练习记录和邀请码（会先自动备份）。" % DB_PATH)
    print("如果服务正在运行，请先停掉它。")
    try:
        answer = input("确认请输入 RESET：")
    except EOFError:
        return False
    return answer.strip() == "RESET"


def init_db(reset=False):
    # 建库这一步也要 WAL：journal_mode 是写进库文件头的永久设置，
    # 在这里落一次，运行时（db.connect）只是每个连接再确认一遍。
    conn = connect_db(DB_PATH)
    conn.row_factory = sqlite3.Row

    if reset:
        db_backup, rec_moved = backup_before_reset(conn)
        if db_backup:
            print("--reset：旧库已备份到 %s" % db_backup)
        if rec_moved:
            print("--reset：旧录音已挪到 %s" % rec_moved)
        print("--reset：删除旧表")
        conn.executescript(DROP)

    conn.executescript(SCHEMA)
    # 老库里的 words 表没有性别分列的音频字段，补列要在扫描 Quiz3000 音频之前
    migrate_words_voice_columns(conn)
    # users 表那四个「练习默认参数」列同理：老库只有 CREATE TABLE IF NOT EXISTS 挡着，不会补
    migrate_users_rhythm_columns(conn)
    # 后端判分要的出题快照列，同样只能靠 ALTER 给老库补上
    migrate_sessions_questions_column(conn)

    words = seed_words(conn)
    scan_vowel_audio(conn)
    sentence_items, sentence_missing = scan_sentence_audio(conn)
    audio_words, orphans = scan_word_audio(conn)
    q3_inserted, q3_updated, q3_seeded, q3_orphans = scan_quiz3000_audio(conn)
    conn.commit()

    if orphans:
        print(
            "  {n} 个单词音频在词库里查不到（没有音标与释义的词不会入库）：{items}".format(
                n=len(orphans), items=" ".join(orphans)
            )
        )
    if q3_orphans:
        print(
            "  Quiz3000 里 {n} 个音频的词形不在词表（词典查不到，按非单词处理）：{items}".format(
                n=len(q3_orphans), items=" ".join(q3_orphans[:40])
            )
        )

    total_groups = conn.execute("SELECT COUNT(*) AS c FROM vowel_groups").fetchone()["c"]
    total_items = conn.execute("SELECT COUNT(*) AS c FROM vowel_items").fetchone()["c"]
    total_sentences = conn.execute("SELECT COUNT(*) AS c FROM sentence_items").fetchone()["c"]
    sentence_groups = conn.execute(
        "SELECT COUNT(DISTINCT group_id) AS c FROM sentence_items").fetchone()["c"]
    bank_size = conn.execute("SELECT COUNT(*) AS c FROM words").fetchone()["c"]
    both_voices = conn.execute(
        "SELECT COUNT(*) AS c FROM words WHERE audio_f IS NOT NULL AND audio_m IS NOT NULL"
    ).fetchone()["c"]
    print(
        "数据库初始化完成 → {path}\n  种子单词 {seed} 条，词库共 {bank} 条"
        "（教材 3A 音频 {wa} 条，Quiz3000 新增 {qn} 条、给种子词补男女声 {qs} 条、"
        "刷新已有 Quiz3000 词条 {qu} 条，男女声齐全 {qv} 条）\n"
        "  近似音组 {g} 组，最小对立音频 {i} 条\n"
        "  句子近似音 {s} 条（覆盖 {sg} 组，缺文本跳过 {sm} 条）".format(
            path=os.path.relpath(DB_PATH, BASE_DIR),
            seed=words,
            bank=bank_size,
            wa=audio_words,
            qn=q3_inserted,
            qs=q3_seeded,
            qu=q3_updated,
            qv=both_voices,
            g=total_groups,
            i=total_items,
            s=total_sentences,
            sg=sentence_groups,
            sm=sentence_missing,
        )
    )
    conn.close()


def parse_args(argv=None):
    # 以前是 "--reset" in sys.argv：--help 会把整个初始化跑一遍，--rest 这种拼错会被静默忽略。
    parser = argparse.ArgumentParser(description="建表、灌种子数据、扫描 static/audio 登记音频。不带参数时幂等。")
    parser.add_argument("--reset", action="store_true",
                        help="先备份库和录音目录，再删掉全部表重建（会清空账号、成绩、邀请码）")
    parser.add_argument("--yes", action="store_true",
                        help="配合 --reset，跳过交互确认（备份照做）")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    if args.yes and not args.reset:
        sys.exit("--yes 只能和 --reset 一起用")
    if args.reset and not confirm_reset(args.yes):
        sys.exit("已取消，什么都没改。")
    init_db(reset=args.reset)
