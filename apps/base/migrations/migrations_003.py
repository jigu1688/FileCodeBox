from tortoise import connections


async def create_collection_box_table():
    conn = connections.get("default")
    await conn.execute_script(
        """
        CREATE TABLE IF NOT EXISTS "collectionbox" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT,
            "code" VARCHAR(255) NOT NULL UNIQUE,
            "name" VARCHAR(255) NOT NULL,
            "description" TEXT,
            "max_file_size" BIGINT DEFAULT 0,
            "allowed_extensions" VARCHAR(255),
            "expired_at" TIMESTAMPTZ,
            "max_files" INTEGER DEFAULT 0,
            "created_at" TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_collectionbox_code ON collectionbox (code);
        
        ALTER TABLE "filecodes" ADD "collection_box_id" INTEGER;
        """
    )


async def migrate():
    await create_collection_box_table()
