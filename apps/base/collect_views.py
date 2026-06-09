import os
import random
import string
import zipfile
import io
import mimetypes
from datetime import datetime
from typing import Optional
from urllib.parse import quote
import asyncio

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel, Field
from pathlib import Path
import uuid

from apps.base.models import CollectionBox, FileCodes
from apps.base.utils import ip_limit, get_file_path_name, get_random_code
from apps.admin.dependencies import admin_required
from core.response import APIResponse
from core.settings import settings, data_root
from core.storage import storages, FileStorageInterface

collect_api = APIRouter(prefix="", tags=["文件收集柜"])


class CollectionBoxCreate(BaseModel):
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    max_file_size: Optional[int] = 0  # 字节为单位，0表示不限制
    allowed_extensions: Optional[str] = None  # 逗号分隔的扩展名，例如 "jpg,png"
    expired_at: Optional[datetime] = None
    max_files: Optional[int] = 0  # 0表示不限制
    code: Optional[str] = Field(None, max_length=20)  # 自定义提取码，最长 20 位


# ==========================================
# 管理员 API (Admin APIs)
# ==========================================

@collect_api.post("/api/admin/collect/create")
async def create_box(data: CollectionBoxCreate, admin: bool = Depends(admin_required)):
    if data.code:
        # 验证自定义提取码格式
        custom_code = data.code.lower().strip()
        if not custom_code.isalnum() or len(custom_code) < 3 or len(custom_code) > 20:
            raise HTTPException(status_code=400, detail="自定义提取码必须为 3-20 位英文或数字")
            
        # 校验唯一性
        if await CollectionBox.filter(code=custom_code).exists() or await FileCodes.filter(code=custom_code).exists():
            raise HTTPException(status_code=400, detail="该提取码已被占用，请使用其他提取码")
        code = custom_code
    else:
        chars = string.ascii_lowercase + string.digits
        while True:
            code = "".join(random.choice(chars) for _ in range(5))
            if not await CollectionBox.filter(code=code).exists() and not await FileCodes.filter(code=code).exists():
                break
    
    box = await CollectionBox.create(
        code=code,
        name=data.name,
        description=data.description,
        max_file_size=data.max_file_size or 0,
        allowed_extensions=data.allowed_extensions,
        expired_at=data.expired_at,
        max_files=data.max_files or 0
    )
    return APIResponse(detail={"code": code, "id": box.id})


@collect_api.get("/api/admin/collect/list")
async def list_boxes(admin: bool = Depends(admin_required)):
    boxes = await CollectionBox.all().order_by("-created_at")
    result = []
    for box in boxes:
        collected_count = await FileCodes.filter(collection_box_id=box.id).count()
        result.append({
            "id": box.id,
            "code": box.code,
            "name": box.name,
            "description": box.description,
            "max_file_size": box.max_file_size,
            "allowed_extensions": box.allowed_extensions,
            "expired_at": box.expired_at.isoformat() if box.expired_at else None,
            "max_files": box.max_files,
            "created_at": box.created_at.isoformat(),
            "collected_count": collected_count
        })
    return APIResponse(detail=result)


@collect_api.get("/api/admin/collect/{box_id}/files")
async def get_box_files(box_id: int, admin: bool = Depends(admin_required)):
    box = await CollectionBox.filter(id=box_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收集箱不存在")
    
    files = await FileCodes.filter(collection_box_id=box_id).order_by("-created_at")
    result = []
    for f in files:
        result.append({
            "id": f.id,
            "code": f.code,
            "prefix": f.prefix,
            "suffix": f.suffix,
            "size": f.size,
            "created_at": f.created_at.isoformat(),
            "download_url": f"/api/admin/collect/file/{f.id}/download"
        })
    return APIResponse(detail={
        "box": {
            "id": box.id,
            "code": box.code,
            "name": box.name,
            "description": box.description
        },
        "files": result
    })


@collect_api.get("/api/admin/collect/file/{file_id}/download")
async def download_collected_file(file_id: int, admin: bool = Depends(admin_required)):
    file_code = await FileCodes.filter(id=file_id).first()
    if not file_code or not file_code.collection_box_id:
        raise HTTPException(status_code=404, detail="文件不存在")
    
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    return await file_storage.get_file_response(file_code)


@collect_api.delete("/api/admin/collect/{box_id}")
async def delete_box(box_id: int, admin: bool = Depends(admin_required)):
    box = await CollectionBox.filter(id=box_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收集箱不存在")
    
    files = await FileCodes.filter(collection_box_id=box_id).all()
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    for f in files:
        try:
            await file_storage.delete_file(f)
        except Exception:
            pass
        await f.delete()
        
    await box.delete()
    return APIResponse(detail="删除成功")


def cleanup_temp_file(file_path: str):
    try:
        path = Path(file_path)
        if path.exists():
            path.unlink()
    except Exception:
        pass


@collect_api.get("/api/admin/collect/{box_id}/zip")
async def download_box_zip(box_id: int, background_tasks: BackgroundTasks, admin: bool = Depends(admin_required)):
    box = await CollectionBox.filter(id=box_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收集箱不存在")
        
    files = await FileCodes.filter(collection_box_id=box_id).all()
    if not files:
        raise HTTPException(status_code=400, detail="该收集箱暂无文件")
        
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    
    # 确保 data/temp_zips 目录存在
    temp_dir = Path(data_root) / "temp_zips"
    if not temp_dir.exists():
        temp_dir.mkdir(parents=True, exist_ok=True)
        
    temp_file_path = temp_dir / f"collect_{box_id}_{uuid.uuid4().hex}.zip"
    
    with zipfile.ZipFile(temp_file_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for f in files:
            filename = f"{f.prefix}{f.suffix}"
            try:
                if settings.file_storage == "local":
                    local_path = file_storage.root_path / await f.get_file_path()
                    if local_path.exists():
                        await asyncio.to_thread(zip_file.write, local_path, filename)
                else:
                    resp = await file_storage.get_file_response(f)
                    content = getattr(resp, "body", b"")
                    await asyncio.to_thread(zip_file.writestr, filename, content)
            except Exception:
                pass
                
    background_tasks.add_task(cleanup_temp_file, str(temp_file_path))
    
    encoded_filename = quote(f"{box.name}_收集文件.zip", safe='')
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        "Accept-Ranges": "bytes"
    }
    return FileResponse(
        path=temp_file_path,
        media_type="application/zip",
        headers=headers
    )


@collect_api.get("/api/admin/collect/{box_id}/zip/prepare")
async def prepare_box_zip(box_id: int, admin: bool = Depends(admin_required)):
    box = await CollectionBox.filter(id=box_id).first()
    if not box:
        raise HTTPException(status_code=404, detail="收集箱不存在")
        
    files = await FileCodes.filter(collection_box_id=box_id).all()
    if not files:
        raise HTTPException(status_code=400, detail="该收集箱暂无文件")
        
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    
    # 确保 data/temp_zips 目录存在
    temp_dir = Path(data_root) / "temp_zips"
    if not temp_dir.exists():
        temp_dir.mkdir(parents=True, exist_ok=True)
        
    zip_name = f"collect_{box_id}_{uuid.uuid4().hex}.zip"
    temp_file_path = temp_dir / zip_name
    
    with zipfile.ZipFile(temp_file_path, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for f in files:
            filename = f"{f.prefix}{f.suffix}"
            try:
                if settings.file_storage == "local":
                    local_path = file_storage.root_path / await f.get_file_path()
                    if local_path.exists():
                        await asyncio.to_thread(zip_file.write, local_path, filename)
                else:
                    resp = await file_storage.get_file_response(f)
                    content = getattr(resp, "body", b"")
                    await asyncio.to_thread(zip_file.writestr, filename, content)
            except Exception:
                pass
                
    return APIResponse(detail={"zip_name": zip_name})


@collect_api.get("/api/admin/collect/zip/download/{zip_name}")
async def download_box_zip_file(zip_name: str, admin: bool = Depends(admin_required)):
    # 路径安全检查，防止目录穿越
    if ".." in zip_name or "/" in zip_name or "\\" in zip_name:
        raise HTTPException(status_code=400, detail="非法的文件名称")
        
    temp_dir = Path(data_root) / "temp_zips"
    temp_file_path = temp_dir / zip_name
    
    if not temp_file_path.exists():
        raise HTTPException(status_code=404, detail="打包文件已过期或不存在，请重新打包下载")
        
    # 根据文件名中的 box_id 获取收集箱名称以构建友好的文件名
    box_name = "收集文件"
    try:
        parts = zip_name.split("_")
        if len(parts) >= 2 and parts[0] == "collect":
            box_id = int(parts[1])
            box = await CollectionBox.filter(id=box_id).first()
            if box:
                box_name = box.name
    except Exception:
        pass
        
    encoded_filename = quote(f"{box_name}_收集文件.zip", safe='')
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}",
        "Accept-Ranges": "bytes"
    }
    return FileResponse(
        path=temp_file_path,
        media_type="application/zip",
        headers=headers
    )


# ==========================================
# 公开 API (Public APIs)
# ==========================================

@collect_api.get("/api/collect/{code}")
async def get_box_info(code: str):
    box = await CollectionBox.filter(code=code).first()
    if not box:
        raise HTTPException(status_code=404, detail="收集箱不存在")
    
    if await box.is_expired():
        return APIResponse(code=400, detail="该收集箱已过期")
        
    collected_count = await FileCodes.filter(collection_box_id=box.id).count()
    if box.max_files > 0 and collected_count >= box.max_files:
        return APIResponse(code=400, detail="该收集箱文件数量已达上限")
        
    return APIResponse(detail={
        "id": box.id,
        "code": box.code,
        "name": box.name,
        "description": box.description,
        "max_file_size": box.max_file_size,
        "allowed_extensions": box.allowed_extensions,
        "expired_at": box.expired_at.isoformat() if box.expired_at else None,
        "max_files": box.max_files,
        "collected_count": collected_count
    })


@collect_api.post("/api/collect/{code}/upload")
async def upload_to_box(
    code: str,
    file: UploadFile = File(...),
    ip: str = Depends(ip_limit["upload"])
):
    box = await CollectionBox.filter(code=code).first()
    if not box:
        raise HTTPException(status_code=404, detail="收集箱不存在")
        
    if await box.is_expired():
        raise HTTPException(status_code=400, detail="该收集箱已过期")
        
    collected_count = await FileCodes.filter(collection_box_id=box.id).count()
    if box.max_files > 0 and collected_count >= box.max_files:
        raise HTTPException(status_code=400, detail="该收集箱文件数量已达上限")
        
    # 验证大小
    max_size = box.max_file_size if box.max_file_size > 0 else settings.uploadSize
    file_size = file.size
    if file_size is None:
        file.file.seek(0, 2)
        file_size = file.file.tell()
        file.file.seek(0)
    if file_size > max_size:
        max_size_mb = max_size / (1024 * 1024)
        raise HTTPException(status_code=400, detail=f"文件大小超过限制，最大允许为 {max_size_mb:.2f} MB")
        
    # 验证后缀名
    filename = file.filename
    _, ext = os.path.splitext(filename)
    ext = ext.lstrip(".").lower()
    
    if box.allowed_extensions:
        allowed_list = [x.strip().lower() for x in box.allowed_extensions.split(",") if x.strip()]
        if ext not in allowed_list:
            raise HTTPException(status_code=400, detail=f"不支持的文件格式。允许的格式：{box.allowed_extensions}")
            
    # 保存文件
    path, suffix, prefix, uuid_file_name, save_path = await get_file_path_name(file)
    file_storage: FileStorageInterface = storages[settings.file_storage]()
    await file_storage.save_file(file, save_path)
    
    # 写入文件提取码（主要为了保持系统底层兼容性且主键唯一）
    file_code = await get_random_code(style="string")
    
    await FileCodes.create(
        code=file_code,
        prefix=prefix,
        suffix=suffix,
        uuid_file_name=uuid_file_name,
        file_path=path,
        size=file_size,
        collection_box_id=box.id,
        expired_at=box.expired_at,
        expired_count=-1
    )
    
    ip_limit["upload"].add_ip(ip)
    return APIResponse(detail={"code": file_code, "name": filename})
