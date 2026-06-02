import glob
import importlib
import os
import re

from tortoise import Tortoise

from core.logger import logger
from core.settings import data_root


def get_db_type(db_url: str) -> str:
    if db_url.startswith("postgres") or db_url.startswith("postgresql"):
        return "postgres"
    elif db_url.startswith("mysql"):
        return "mysql"
    return "sqlite"


def translate_sql(sql: str, db_type: str) -> str:
    if db_type == "sqlite":
        return sql

    # Universal: Strip double quotes around table and column names to ensure MySQL/Postgres cross-compatibility
    sql = sql.replace('"', '')

    if db_type == "mysql":
        # 1. Replace AUTOINCREMENT with AUTO_INCREMENT
        sql = re.sub(r'\bAUTOINCREMENT\b', 'AUTO_INCREMENT', sql, flags=re.IGNORECASE)
        # 2. Replace TIMESTAMPTZ with TIMESTAMP since MySQL doesn't support TIMESTAMPTZ
        sql = sql.replace("TIMESTAMPTZ", "TIMESTAMP")
        return sql

    if db_type == "postgres":
        # 1. id INTEGER PRIMARY KEY AUTOINCREMENT -> id SERIAL PRIMARY KEY (handling multiple spaces and optional NOT NULL)
        sql = re.sub(
            r'\bid\s+INTEGER\s+(?:NOT\s+NULL\s+)?PRIMARY\s+KEY\s+AUTOINCREMENT\b',
            'id SERIAL PRIMARY KEY',
            sql,
            flags=re.IGNORECASE
        )
        # 2. Double check if any raw "PRIMARY KEY AUTOINCREMENT" is left and replace it
        sql = re.sub(
            r'\bPRIMARY\s+KEY\s+AUTOINCREMENT\b',
            'PRIMARY KEY',
            sql,
            flags=re.IGNORECASE
        )
        return sql

    return sql


def format_placeholder(query_str: str, db_type: str) -> str:
    if db_type == "postgres":
        # Replace ? with $1, $2, $3, etc.
        count = [0]
        def repl(match):
            count[0] += 1
            return f"${count[0]}"
        return re.sub(r'\?', repl, query_str)
    elif db_type == "mysql":
        return query_str.replace("?", "%s")
    return query_str


async def init_db():
    try:
        # 从环境变量或本地配置加载DB URL
        db_url = os.getenv("DATABASE_URL") or f"sqlite://{data_root}/filecodebox.db"
        db_type = get_db_type(db_url)

        # 校验对应驱动依赖库
        if db_type == "postgres":
            try:
                import asyncpg
            except ImportError:
                logger.error("【错误】检测到配置了 PostgreSQL 数据库，但未安装 asyncpg 驱动！")
                logger.error("【解决办法】请在运行前执行: pip install asyncpg")
                raise RuntimeError("PostgreSQL driver 'asyncpg' is not installed. Please run 'pip install asyncpg'")
        elif db_type == "mysql":
            try:
                import aiomysql
            except ImportError:
                logger.error("【错误】检测到配置了 MySQL 数据库，但未安装 aiomysql 驱动！")
                logger.error("【解决办法】请在运行前执行: pip install aiomysql")
                raise RuntimeError("MySQL driver 'aiomysql' is not installed. Please run 'pip install aiomysql'")

        # 使用正确的Tortoise初始化配置格式
        db_config = {
            "db_url": db_url,
            "modules": {"models": ["apps.base.models"]},
            "use_tz": False,
            "timezone": "Asia/Shanghai"
        }

        import inspect
        sig = inspect.signature(Tortoise.init)
        if "_enable_global_fallback" in sig.parameters:
            await Tortoise.init(_enable_global_fallback=True, **db_config)
        else:
            await Tortoise.init(**db_config)

        # 动态劫持/代理连接方法，实现数据库方言的无缝适配与分割执行
        conn = Tortoise.get_connection("default")
        original_execute_script = conn.execute_script
        original_execute_query = conn.execute_query

        async def patched_execute_script(script_str, *args, **kwargs):
            translated = translate_sql(script_str, db_type)
            # 分割 SQL 脚本为多条独立 SQL 语句，依次串行执行，彻底绕过不同驱动对多语句执行的限制
            statements = []
            for stmt in translated.split(";"):
                stmt_clean = stmt.strip()
                if stmt_clean:
                    statements.append(stmt_clean)
            for stmt in statements:
                await original_execute_script(stmt, *args, **kwargs)

        async def patched_execute_query(query_str, *args, **kwargs):
            translated_query = translate_sql(query_str, db_type)
            translated_query = format_placeholder(translated_query, db_type)
            return await original_execute_query(translated_query, *args, **kwargs)

        # 覆盖连接对象方法
        conn.execute_script = patched_execute_script
        conn.execute_query = patched_execute_query

        # 创建migrations表
        await conn.execute_script("""
            CREATE TABLE IF NOT EXISTS migrates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                migration_file VARCHAR(255) NOT NULL UNIQUE,
                executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 执行迁移
        await execute_migrations()

    except Exception as e:
        logger.error(f"数据库初始化失败: {str(e)}")
        raise


async def execute_migrations():
    """执行数据库迁移"""
    try:
        # 收集迁移文件
        migration_files = []
        for root, dirs, files in os.walk("apps"):
            if "migrations" in dirs:
                migration_path = os.path.join(root, "migrations")
                migration_files.extend(glob.glob(os.path.join(migration_path, "migrations_*.py")))

        # 按文件名排序
        migration_files.sort()

        for migration_file in migration_files:
            file_name = os.path.basename(migration_file)

            # 检查是否已执行
            executed = await Tortoise.get_connection("default").execute_query(
                "SELECT id FROM migrates WHERE migration_file = ?", [file_name]
            )

            if not executed[1]:
                logger.info(f"执行迁移: {file_name}")
                # 导入并执行migration
                module_path = migration_file.replace("/", ".").replace("\\", ".").replace(".py", "")
                try:
                    migration_module = importlib.import_module(module_path)
                    if hasattr(migration_module, "migrate"):
                        await migration_module.migrate()
                        # 记录执行
                        await Tortoise.get_connection("default").execute_query(
                            "INSERT INTO migrates (migration_file) VALUES (?)",
                            [file_name]
                        )
                        logger.info(f"迁移完成: {file_name}")
                except Exception as e:
                    logger.error(f"迁移 {file_name} 执行失败: {str(e)}")
                    raise

    except Exception as e:
        logger.error(f"迁移过程发生错误: {str(e)}")
        raise
