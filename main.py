# @Time    : 2023/8/9 23:23
# @Author  : Lan
# @File    : main.py
# @Software: PyCharm
import asyncio
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from tortoise import Tortoise
from tortoise.contrib.fastapi import register_tortoise

from apps.admin.views import admin_api
from apps.base.models import KeyValue
from apps.base.utils import ip_limit
from apps.base.views import share_api, chunk_api
from apps.base.collect_views import collect_api
from core.database import init_db
from core.logger import logger
from core.response import APIResponse
from core.settings import data_root, settings, BASE_DIR, DEFAULT_CONFIG
from core.tasks import delete_expire_files, clean_incomplete_uploads

from fastapi import HTTPException

class FallbackStaticFiles(StaticFiles):
    def __init__(self, directory: str, fallback_directory: str, **kwargs):
        super().__init__(directory=directory, **kwargs)
        self.fallback_staticfiles = StaticFiles(directory=fallback_directory, **kwargs)

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
            if response.status_code == 404:
                return await self.fallback_staticfiles.get_response(path, scope)
            return response
        except HTTPException as e:
            if e.status_code == 404:
                try:
                    return await self.fallback_staticfiles.get_response(path, scope)
                except Exception:
                    raise e
            raise e
        except Exception as e:
            try:
                return await self.fallback_staticfiles.get_response(path, scope)
            except Exception:
                raise e


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("正在初始化应用...")
    # 初始化数据库
    await init_db()

    # 加载配置
    await load_config()

    # 清理遗留的临时压缩包
    temp_zip_dir = data_root / "temp_zips"
    if temp_zip_dir.exists():
        import shutil
        try:
            shutil.rmtree(temp_zip_dir)
            logger.info("已清理遗留的临时压缩文件目录")
        except Exception as e:
            logger.warning(f"清理遗留临时压缩文件目录失败: {e}")
    app.mount(
        "/assets",
        FallbackStaticFiles(directory=f"./{settings.themesSelect}/assets", fallback_directory="./themes/2024/assets"),
        name="assets",
    )
    app.mount(
        "/assets-2024",
        StaticFiles(directory="./themes/2024/assets"),
        name="assets-2024",
    )

    # 启动后台任务
    task = asyncio.create_task(delete_expire_files())
    chunk_cleanup_task = asyncio.create_task(clean_incomplete_uploads())
    logger.info("应用初始化完成")

    try:
        yield
    finally:
        # 清理操作
        logger.info("正在关闭应用...")
        task.cancel()
        chunk_cleanup_task.cancel()
        await asyncio.gather(task, chunk_cleanup_task, return_exceptions=True)
        await Tortoise.close_connections()
        logger.info("应用已关闭")


async def load_config():
    user_config, _ = await KeyValue.get_or_create(
        key="settings", defaults={"value": DEFAULT_CONFIG}
    )
    await KeyValue.update_or_create(
        key="sys_start", defaults={"value": int(time.time() * 1000)}
    )
    config_dict = user_config.value or {}
    changed = False
    for k, v in DEFAULT_CONFIG.items():
        if k not in config_dict:
            config_dict[k] = v
            changed = True

    if not config_dict.get("jwt_secret"):
        import secrets
        config_dict["jwt_secret"] = secrets.token_urlsafe(32)
        changed = True

    # Ensure 2026 is in themesChoices if it's missing (to avoid old db values locking it out)
    themes_list = config_dict.get("themesChoices", [])
    if not any(t.get("name") == "2026" for t in themes_list):
        themes_list.append({
            "name": "2026",
            "key": "themes/2026",
            "author": "Antigravity",
            "version": "1.0",
        })
        config_dict["themesChoices"] = themes_list
        changed = True

    if changed:
        user_config.value = config_dict
        await user_config.save()

    settings.user_config = config_dict
    # 更新 ip_limit 配置
    ip_limit["error"].minutes = settings.errorMinute
    ip_limit["error"].count = settings.errorCount
    ip_limit["upload"].minutes = settings.uploadMinute
    ip_limit["upload"].count = settings.uploadCount


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 使用 register_tortoise 来添加异常处理器
register_tortoise(
    app,
    config={
        "connections": {"default": f"sqlite://{data_root}/filecodebox.db"},
        "apps": {
            "models": {
                "models": ["apps.base.models"],
                "default_connection": "default",
            },
        },
    },
    generate_schemas=False,
    add_exception_handlers=True,
)

app.include_router(share_api)
app.include_router(chunk_api)
app.include_router(admin_api)
app.include_router(collect_api)


@app.get("/collect/admin")
async def collect_admin():
    return HTMLResponse(
        content=open(BASE_DIR / "themes/collect_admin.html", "r", encoding="utf-8").read(),
        media_type="text/html"
    )


@app.get("/collect/{code}")
async def collect_page(code: str):
    return HTMLResponse(
        content=open(BASE_DIR / "themes/collect.html", "r", encoding="utf-8").read(),
        media_type="text/html"
    )


@app.get("/preview/{code}")
async def file_preview(code: str, key: str):
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse
    from apps.base.models import FileCodes
    from core.utils import get_select_token
    from core.settings import settings, BASE_DIR
    from core.storage import storages
    import datetime

    # 1. 鉴权校验
    if await get_select_token(code) != key:
        raise HTTPException(status_code=403, detail="预览鉴权失败")

    # 2. 获取文件记录
    file_code = await FileCodes.filter(code=code).first()
    if not file_code:
        raise HTTPException(status_code=404, detail="文件不存在")

    # 3. 确定预览类型
    filename = f"{file_code.prefix}{file_code.suffix}"
    ext = file_code.suffix.lower().strip(".")
    
    # 支持的分类
    img_exts = {"jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"}
    video_exts = {"mp4", "webm"}
    audio_exts = {"mp3", "wav", "ogg"}
    pdf_exts = {"pdf"}
    
    code_exts = {
        "txt", "py", "js", "css", "html", "json", "java", "go",
        "c", "cpp", "sh", "rs", "yaml", "yml", "ini", "conf", "sql"
    }
    
    preview_type = "other"
    if ext in img_exts:
        preview_type = "image"
    elif ext in video_exts:
        preview_type = "video"
    elif ext in audio_exts:
        preview_type = "audio"
    elif ext in pdf_exts:
        preview_type = "pdf"
    elif ext == "md":
        preview_type = "markdown"
    elif ext in code_exts:
        preview_type = "code"

    # 4. 获取下载链接与内容
    download_url = f"/share/download?key={key}&code={code}"
    
    file_content_escaped = ""
    if preview_type in ["code", "markdown"]:
        try:
            file_storage = storages[settings.file_storage]()
            if settings.file_storage == "local":
                file_path = file_storage.root_path / await file_code.get_file_path()
                if file_path.exists():
                    file_content_escaped = file_path.read_text(encoding="utf-8", errors="ignore")
                else:
                    file_content_escaped = "文件在磁盘上不存在"
            else:
                resp = await file_storage.get_file_response(file_code)
                if hasattr(resp, "body") and resp.body:
                    file_content_escaped = resp.body.decode("utf-8", errors="ignore")
                elif hasattr(resp, "body_iterator"):
                    chunks = []
                    async for chunk in resp.body_iterator:
                        chunks.append(chunk)
                    file_content_escaped = b"".join(chunks).decode("utf-8", errors="ignore")
                else:
                    file_content_escaped = "该云存储模式暂不支持直接读取预览，请直接下载"
        except Exception as e:
            file_content_escaped = f"读取文件内容失败: {str(e)}"
            
        file_content_escaped = (
            file_content_escaped
            .replace("\\", "\\\\")
            .replace("`", "\\`")
            .replace("$", "\\$")
        )

    # 5. 读取 preview.html 并填入数据
    template_path = BASE_DIR / "themes/preview.html"
    if not template_path.exists():
        raise HTTPException(status_code=500, detail="预览模板不存在")
        
    html_content = template_path.read_text(encoding="utf-8")
    
    size_mb = f"{file_code.size / (1024 * 1024):.2f} MB" if file_code.size else "未知大小"
    created_str = file_code.created_at.strftime("%Y-%m-%d %H:%M:%S") if file_code.created_at else "未知时间"

    html_content = (
        html_content
        .replace("{{code}}", code)
        .replace("{{key}}", key)
        .replace("{{filename}}", filename)
        .replace("{{size}}", size_mb)
        .replace("{{created_at}}", created_str)
        .replace("{{preview_type}}", preview_type)
        .replace("{{download_url}}", download_url)
        .replace("{{file_content_escaped}}", file_content_escaped)
    )

    return HTMLResponse(content=html_content, media_type="text/html")


@app.exception_handler(404)
@app.get("/")
async def index(request=None, exc=None):
    return HTMLResponse(
        content=open(
            BASE_DIR / f"{settings.themesSelect}/index.html", "r", encoding="utf-8"
        )
        .read()
        .replace("{{title}}", str(settings.name))
        .replace("{{description}}", str(settings.description))
        .replace("{{keywords}}", str(settings.keywords))
        .replace("{{opacity}}", str(settings.opacity))
        .replace('"/assets/', '"assets/')
        .replace("{{background}}", str(settings.background)),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/sys-admin")
async def sys_admin():
    html_content = open(
        BASE_DIR / "themes/2024/index.html", "r", encoding="utf-8"
    ).read()
    
    html_content = (
        html_content
        .replace("{{title}}", str(settings.name))
        .replace("{{description}}", str(settings.description))
        .replace("{{keywords}}", str(settings.keywords))
        .replace("{{opacity}}", str(settings.opacity))
        .replace('"/assets/', '"/assets-2024/')
        .replace("{{background}}", str(settings.background))
    )
    return HTMLResponse(
        content=html_content,
        media_type="text/html",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"}
    )


@app.get("/robots.txt")
async def robots():
    return HTMLResponse(content=settings.robotsText, media_type="text/plain")


@app.post("/")
async def get_config():
    return APIResponse(
        detail={
            "name": settings.name,
            "description": settings.description,
            "explain": settings.page_explain,
            "uploadSize": settings.uploadSize,
            "expireStyle": settings.expireStyle,
            "enableChunk": settings.enableChunk,
            "openUpload": settings.openUpload,
            "notify_title": settings.notify_title,
            "notify_content": settings.notify_content,
            "show_admin_address": settings.showAdminAddr,
            "max_save_seconds": settings.max_save_seconds,
        }
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app="main:app", host=settings.serverHost, port=settings.serverPort, reload=False, workers=settings.serverWorkers
    )
