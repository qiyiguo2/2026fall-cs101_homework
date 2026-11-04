import os

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-fallback")
DATABASE_PATH = os.environ.get("DATABASE_PATH", "data/app.db")
AUDIO_DIR = os.environ.get("AUDIO_DIR", "static/audio")
RECORDING_DIR = os.environ.get("RECORDING_DIR", "data/recordings")





DEFAULT_QUESTION_COUNT = 20
DEFAULT_TIME_LIMIT = 1      # 近似音区分每题默认秒数
MAX_QUESTION_COUNT = 100    # 防止用户输入离谱的值


DEBUG = os.environ.get("FLASK_DEBUG", "false").lower() == "true"