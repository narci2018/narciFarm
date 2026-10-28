import os
import tempfile

os.environ["NARCI_TEST_MODE"] = "1"
# 必须在任何测试模块导入 narci.db 之前指向临时目录，否则会连到真实 data/narci.db，
# 导致测试之间通过全局 DB 连接互相污染（表现为登录失败等随机错误）。
os.environ.setdefault("NARCI_DATA_DIR", tempfile.mkdtemp(prefix="narci_test_"))
