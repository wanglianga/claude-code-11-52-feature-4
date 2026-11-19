import os

DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev"
)
TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://ccdev:ccdev@127.0.0.1:5432/ccdev_test",
)

# 观察窗口粒度（分钟），所有源数据均以 15 分钟为采样承诺单位
SLOT_MINUTES = 15
WINDOW_HOURS = 24
# 近实时产品允许的暂定展示滞后（分钟）：数据截止点之后仍可展示，但带暂定标记
REALTIME_GRACE_MINUTES = 60
