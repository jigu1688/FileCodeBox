# @Time    : 2023/8/13 20:43
# @Author  : Lan
# @File    : models.py
# @Software: PyCharm
from typing import Optional

from tortoise.models import Model
from tortoise.contrib.pydantic import pydantic_model_creator

from tortoise import fields, models
from datetime import datetime
from core.utils import get_now


class FileCodes(models.Model):
    id = fields.IntField(pk=True)
    code = fields.CharField(max_length=255, unique=True, index=True)
    prefix = fields.CharField(max_length=255, default="")
    suffix = fields.CharField(max_length=255, default="")
    uuid_file_name = fields.CharField(max_length=255, null=True)
    file_path = fields.CharField(max_length=255, null=True)
    size = fields.IntField(default=0)
    text = fields.TextField(null=True)
    expired_at = fields.DatetimeField(null=True)
    expired_count = fields.IntField(default=0)
    used_count = fields.IntField(default=0)
    created_at = fields.DatetimeField(auto_now_add=True)
    file_hash = fields.CharField(max_length=64, null=True)
    is_chunked = fields.BooleanField(default=False)
    upload_id = fields.CharField(max_length=36, null=True)
    collection_box_id = fields.IntField(null=True, index=True)

    async def is_expired(self):
        if self.expired_at is None:
            return False
        if self.expired_at and self.expired_count < 0:
            now = await get_now()
            expired_at = self.expired_at
            if expired_at.tzinfo is None and now.tzinfo is not None:
                now = now.replace(tzinfo=None)
            elif expired_at.tzinfo is not None and now.tzinfo is None:
                now = now.replace(tzinfo=expired_at.tzinfo)
            return expired_at < now
        return self.expired_count <= 0

    async def get_file_path(self):
        return f"{self.file_path}/{self.uuid_file_name}"


class CollectionBox(models.Model):
    id = fields.IntField(pk=True)
    code = fields.CharField(max_length=255, unique=True, index=True)
    name = fields.CharField(max_length=255)
    description = fields.TextField(null=True)
    max_file_size = fields.BigIntField(default=0)  # 0 means unlimited
    allowed_extensions = fields.CharField(max_length=255, null=True)  # comma separated
    expired_at = fields.DatetimeField(null=True)
    max_files = fields.IntField(default=0)  # 0 means unlimited
    created_at = fields.DatetimeField(auto_now_add=True)

    async def is_expired(self):
        if self.expired_at is None:
            return False
        now = await get_now()
        expired_at = self.expired_at
        if expired_at.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        elif expired_at.tzinfo is not None and now.tzinfo is None:
            now = now.replace(tzinfo=expired_at.tzinfo)
        return expired_at < now


class UploadChunk(models.Model):
    id = fields.IntField(pk=True)
    upload_id = fields.CharField(max_length=36, index=True)
    chunk_index = fields.IntField()
    chunk_hash = fields.CharField(max_length=64)
    total_chunks = fields.IntField()
    file_size = fields.BigIntField()
    chunk_size = fields.IntField()
    file_name = fields.CharField(max_length=255)
    created_at = fields.DatetimeField(auto_now_add=True)
    completed = fields.BooleanField(default=False)


class KeyValue(Model):
    id: Optional[int] = fields.IntField(pk=True)
    key: Optional[str] = fields.CharField(
        max_length=255, description="键", index=True, unique=True
    )
    value: Optional[str] = fields.JSONField(description="值", null=True)
    created_at: Optional[datetime] = fields.DatetimeField(
        auto_now_add=True, description="创建时间"
    )


file_codes_pydantic = pydantic_model_creator(FileCodes, name="FileCodes")
upload_chunk_pydantic = pydantic_model_creator(UploadChunk, name="UploadChunk")
key_value_pydantic = pydantic_model_creator(KeyValue, name="KeyValue")
collection_box_pydantic = pydantic_model_creator(CollectionBox, name="CollectionBox")
