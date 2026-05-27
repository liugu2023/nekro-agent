from pathlib import Path

from tortoise import Tortoise
from tzlocal import get_localzone

from nekro_agent.core.logger import get_sub_logger

from .args import Args
from .core_utils import gen_sqlite_db_url
from .os_env import OsEnv
from .tortoise_config import TORTOISE_ORM, resolve_db_url

logger = get_sub_logger("database")
DB_INITED: bool = False

db_url: str = ""


async def init_db():
    global DB_INITED

    if Args.LOAD_TEST:
        db_url = gen_sqlite_db_url(".temp/load_test.db")
    elif OsEnv.CLI_MODE:
        cli_db_path = Path(OsEnv.DATA_DIR) / "system" / "cli_runtime.db"
        cli_db_path.parent.mkdir(parents=True, exist_ok=True)
        db_url = gen_sqlite_db_url(str(cli_db_path))
    else:
        db_url = resolve_db_url()

    if DB_INITED:
        return

    tortoise_config = dict(TORTOISE_ORM)
    tortoise_config["connections"] = {"default": db_url}
    await Tortoise.init(
        config=tortoise_config,
        timezone=str(get_localzone()),
    )
    DB_INITED = True
    logger.success("Nekro Agent 数据库初始化成功 =^_^=")
