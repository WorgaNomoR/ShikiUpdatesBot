import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("BOT_TOKEN", "test-token")
os.environ.setdefault("OWNER_ID", "123456")

# Изолированная папка данных — чтобы тесты не лезли в реальный /data
_test_data_dir = Path(tempfile.gettempdir()) / "shikibot_test_data"
_test_data_dir.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("DATA_DIR", str(_test_data_dir))