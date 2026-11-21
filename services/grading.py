# services/grading.py
"""后端判分：服务器模式下成绩只以这里算出来的为准。

以前 /api/practice/finish 直接采用前端传来的 is_correct，打开开发者工具交 100 条
is_correct: true 就是 100 分；只交自己答对的那几题，正确率也是 100%。现在的做法：

1. 出题接口（/api/vowel/questions、/api/words）带上 session_id 时，把这场发出去的题
   连同标准答案快照进 practice_sessions.questions_json（见 snapshot_* 两个函数）。
2. 交卷时前端只有「第几题、答了什么」被采信：题目内容和标准答案取快照，
   对错由 judge() 重算，没交上来的题按无回答算错，总题数等于发出去的题数。

前端（vowel.js、tworounds.js）仍然各自判一遍，只用于作答当下的即时反馈和计分板；
本地模式（APP_MODE=local）成绩只存学生自己的浏览器，也仍然由前端判。
所以看读译的中文判分规则在 tworounds.js 的 judgeChinese 里还有一份，**两边必须一起改**，
tests/test_server_grading.py 里有一组两边都应成立的用例。
"""
import re
import unicodedata

# 前端在第二轮超时没输入时交上来的占位文字（tworounds.js 的 cfg.emptyAnswer）。
# 判分前要先认出来：「无回答」三个字本身可能命中某个义项（比如释义里有「回答」）。
EMPTY_ANSWER = "无回答"

# ---------------------------------------------------------------- 看读译：中文释义

# 与 tworounds.js 的 POS_PREFIX 一致：ECDICT 那批释义带词性前缀，如「n. 扫除；vt. 掸去」
POS_PREFIX = re.compile(
    r"(^|[\s；;，,、])(?:n|vt|vi|v|a|adj|adv|ad|pron|prep|num|conj|interj|int|aux|art|abbr|pl|pref)\.\s*",
    re.IGNORECASE,
)
DOMAIN_TAG = re.compile(r"\[[^\]]*\]")                 # [计] [口] 这类领域标签
BRACKETS = re.compile(r"（[^）]*）|\([^)]*\)")          # 括号里的补充说明
SENSE_SPLIT = re.compile(r"[；;／/，,、]")
ELLIPSIS = re.compile(r"…+|\.{2,}|[～~]")


def normalize_sense(text):
    """去掉省略号、波浪线、空白和标点（对应 JS 的 /[\\s\\p{P}]/gu）。"""
    text = ELLIPSIS.sub("", text)
    return "".join(ch for ch in text
                   if not ch.isspace() and not unicodedata.category(ch).startswith("P"))


def chinese_senses(meaning):
    text = DOMAIN_TAG.sub(" ", str(meaning or ""))
    text = BRACKETS.sub(" ", text)
    text = POS_PREFIX.sub(r"\1", text)
    return [s for s in (normalize_sense(part) for part in SENSE_SPLIT.split(text)) if s]


def judge_chinese(meaning, answer):
    """中文译文「任写一项即可」：与某个义项相同，或只差一点点（「满意的」对「满意」）。

    包含关系要求短的一方至少两个字、且不短于长的一方一半：一个字、一个助词、
    或者把义项夹在一长句里，都不算对。规则与 tworounds.js 的 judgeChinese 相同。
    """
    text = normalize_sense(str(answer or ""))
    if not text:
        return False
    for sense in chinese_senses(meaning):
        if sense == text:
            return True
        shorter, longer = (sense, text) if len(sense) <= len(text) else (text, sense)
        if len(shorter) >= 2 and shorter in longer and len(shorter) * 2 >= len(longer):
            return True
    return False


# ---------------------------------------------------------------- 判分入口

def _text_answer(answer):
    """把看读译 / 听读写的作答规整成去掉首尾空白的文本；没答返回空串。"""
    if answer is None or isinstance(answer, bool):
        return ""
    text = str(answer).strip()
    return "" if text == EMPTY_ANSWER else text


def _vowel_choice(answer):
    """近似音的作答是选项编号 1/2；超时是 null。其他写法一律当没答。"""
    if answer is None or isinstance(answer, bool):
        return None
    try:
        return int(str(answer).strip())
    except ValueError:
        return None


def judge(question, answer):
    """按发出去的那道题（快照）判一条作答，返回 True / False。"""
    qtype = question.get("type")
    if qtype == "vowel":
        choice = _vowel_choice(answer)
        return choice is not None and choice == question.get("answer")
    text = _text_answer(answer)
    if not text:
        return False
    if qtype == "read-translate":
        return judge_chinese(question.get("meaning_cn"), text)
    if qtype == "listen-spell":
        return text.lower() == str(question.get("word") or "").lower()
    return False


# ---------------------------------------------------------------- 出题快照
# 快照里存的就是结果页要画的字段（见 templates/result.html），交卷时原样写进
# practice_records.question_json —— 结果页显示的题目和答案也不再取自前端。

def snapshot_vowel(question, mode):
    return {
        "seq": question["seq"],
        "type": "vowel",
        "mode": mode,
        "group_key": question.get("group_key"),
        "options": question["options"],
        "answer": question["answer"],
        "voice": question.get("voice"),
        "audio_url": question.get("audio_url"),
    }


def snapshot_word(seq, word, practice_type):
    return {
        "seq": seq,
        "type": practice_type,
        "word": word["word"],
        "phonetic": word.get("phonetic"),
        "meaning_cn": word.get("meaning_cn"),
        "example": word.get("example"),
        "audio_url": word.get("audio_url"),
        "voice": word.get("voice"),
    }
