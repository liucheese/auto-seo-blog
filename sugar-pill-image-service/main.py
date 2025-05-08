from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import asyncio
import requests
import json
import os
import time
import uuid
from datetime import datetime
import httpx
import logging
import aiofiles
from dotenv import load_dotenv
import sys
import boto3
from botocore.exceptions import NoCredentialsError
import io
from PIL import Image


# 加载环境变量
load_dotenv()

# 配置日志处理
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(os.path.join(os.path.dirname(__file__), 'app.log'))
    ]
)
logger = logging.getLogger(__name__)

# 任务配置
TASK_CONFIG = {
    "max_wait_seconds": 300,
    "check_interval": 3,
    "http_timeout": 10,
    "max_retries": 3
}

# 环境变量或默认值
API_KEY = os.getenv("DASHSCOPE_API_KEY")
IMAGE_STORAGE_DIR = os.getenv("IMAGE_STORAGE_DIR")
PUBLIC_URL_BASE = os.getenv("PUBLIC_URL_BASE")
S3_BUCKET = os.getenv("S3_BUCKET", "sparkle-web-static")
S3_PREFIX = os.getenv("S3_PREFIX", "starrybook/image/blogs/")
S3_REGION = os.getenv("S3_REGION", "ap-southeast-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

# 检查环境变量是否成功加载
if not API_KEY:
    logger.error("未找到 DASHSCOPE_API_KEY 环境变量")
    API_KEY = "sk-d96cd488a8f14e8e87650c62186aead9"  # 使用默认值
    logger.info(f"使用默认 API_KEY: {API_KEY}")

if not IMAGE_STORAGE_DIR:
    logger.error("未找到 IMAGE_STORAGE_DIR 环境变量")
    IMAGE_STORAGE_DIR = os.path.join(os.path.dirname(__file__), 'images')
    logger.info(f"使用默认存储目录: {IMAGE_STORAGE_DIR}")

if not PUBLIC_URL_BASE:
    logger.error("未找到 PUBLIC_URL_BASE 环境变量")
    PUBLIC_URL_BASE = "http://118.178.87.173:8000"
    logger.info(f"使用默认公共URL: {PUBLIC_URL_BASE}")

# 初始化FastAPI应用（关键修改点）
app = FastAPI(
    title="bailian_image_service",
    version="0.1.0",
    servers=[{"url": PUBLIC_URL_BASE, "description": "生产环境API服务"}],
    openapi_url="/openapi.json"
)

@app.on_event("startup")
async def init():
    """初始化任务跟踪集合"""
    logger.info("服务正在启动，初始化任务跟踪集合")
    app.state.task_set = set()
    logger.info(f"当前环境配置: API_KEY长度={len(API_KEY) if API_KEY else 0}, 存储目录={IMAGE_STORAGE_DIR}, PUBLIC_URL={PUBLIC_URL_BASE}")

# 静态文件挂载
app.mount("/images", StaticFiles(directory=IMAGE_STORAGE_DIR), name="images")

# 创建存储目录
os.makedirs(IMAGE_STORAGE_DIR, exist_ok=True)
logger.info(f"确保图片存储目录存在: {IMAGE_STORAGE_DIR}")

# API端点
CREATE_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis"
QUERY_TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}"

# 任务状态跟踪
task_status = {}


class ImageRequest(BaseModel):
    prompt: str = Field(..., description="图像生成提示词", example="一只可爱的猫咪在草地上玩耍")
    negative_prompt: str = Field("", description="负面提示词，指定不希望出现的内容", example="模糊, 低质量")
    model: str = Field("wanx2.1-t2i-turbo", description="使用的模型名称", example="wanx2.1-t2i-turbo")
    size: str = Field("1024*1024", description="图像尺寸", example="1024*1024")
    n: int = Field(1, description="生成图像数量", ge=1, le=4)

class ImageResponse(BaseModel):
    task_id: str
    status: str
    image_urls: List[str] = []
    error: Optional[str] = None


async def create_image_task(request: ImageRequest) -> str:
    """创建阿里云图像生成异步任务"""
    logger.info(f"开始创建图像任务，提示词: {request.prompt}")
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-DashScope-Async": "enable"
    }
    payload = {
        "model": request.model,
        "input": {"prompt": request.prompt},
        "parameters": {"n": request.n, "size": request.size}
    }
    if request.negative_prompt:
        payload["input"]["negative_prompt"] = request.negative_prompt
    
    logger.debug(f"API请求头: Authorization=Bearer *****, 其他头信息已设置")
    logger.debug(f"API请求体: {json.dumps(payload)}")
    
    try:
        async with httpx.AsyncClient() as client:
            logger.info(f"发送请求到阿里云API: {CREATE_TASK_URL}")
            response = await client.post(
                CREATE_TASK_URL,
                headers=headers,
                json=payload,
                timeout=30.0
            )
            logger.info(f"阿里云API响应状态码: {response.status_code}")
            response_json = response.json()
            logger.debug(f"阿里云API响应内容: {json.dumps(response_json)}")
            response.raise_for_status()
            task_id = response_json["output"]["task_id"]
            logger.info(f"成功创建任务，任务ID: {task_id}")
            return task_id
    except httpx.TimeoutException:
        logger.error("请求阿里云API超时")
        raise HTTPException(status_code=504, detail="请求阿里云API超时")
    except httpx.HTTPError as e:
        logger.error(f"请求阿里云API失败: {str(e)}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"错误响应内容: {e.response.text}")
        raise HTTPException(status_code=500, detail=f"请求阿里云API失败: {str(e)}")
    except Exception as e:
        logger.error(f"创建图像任务时发生未知错误: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"未知错误: {str(e)}")

async def query_task(task_id: str) -> Dict[str, Any]:
    """查询阿里云任务状态"""
    logger.info(f"开始查询任务状态，任务ID: {task_id}")
    try:
        async with httpx.AsyncClient() as client:
            url = QUERY_TASK_URL.format(task_id=task_id)
            logger.info(f"发送请求到: {url}")
            response = await client.get(
                url,
                headers={"Authorization": f"Bearer {API_KEY}"},
                timeout=10.0
            )
            logger.info(f"查询响应状态码: {response.status_code}")
            response_json = response.json()
            logger.debug(f"查询响应内容: {json.dumps(response_json)}")
            response.raise_for_status()
            return response_json
    except httpx.TimeoutException:
        logger.error("查询任务状态超时")
        raise HTTPException(status_code=504, detail="查询任务状态超时")
    except httpx.HTTPError as e:
        logger.error(f"查询任务状态错误: {str(e)}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"错误响应内容: {e.response.text}")
        raise HTTPException(status_code=500, detail=f"查询任务状态错误: {str(e)}")
    except Exception as e:
        logger.error(f"查询任务状态时发生未知错误: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"未知错误: {str(e)}")

async def save_images(task_result: Dict[str, Any], prompt: str) -> List[str]:
    logger.info(f"开始处理图片，任务结果包含结果数: {len(task_result.get('output', {}).get('results', []))}")
    s3_urls = []
    if "output" not in task_result or not task_result["output"].get("results"):
        logger.warning("任务结果中没有图片")
        return s3_urls

    logger.info(f"找到 {len(task_result['output']['results'])} 张图片")

    # 使用环境变量中的 S3 配置
    s3_bucket = S3_BUCKET
    s3_prefix = S3_PREFIX
    s3_region = S3_REGION
    try:
        s3_client = boto3.client(
            's3', 
            region_name=s3_region,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY
        )
        logger.info(f"成功初始化S3客户端，区域: {s3_region}")
    except Exception as e:
        logger.error(f"初始化S3客户端失败: {str(e)}", exc_info=True)
        return s3_urls

    # 下载并处理图片，生成三种规格并上传
    async with httpx.AsyncClient() as client:
        for i, result in enumerate(task_result["output"]["results"]):
            if not result.get("url"):
                logger.warning(f"结果 #{i} 中没有URL字段")
                continue
            img_url = result["url"]
            logger.info(f"开始下载图片 #{i}: {img_url}")
            retry_count = 0
            max_retries = 3
            while retry_count < max_retries:
                try:
                    img_response = await client.get(img_url, timeout=15.0)
                    img_response.raise_for_status()
                    logger.info(f"成功下载图片 #{i}, 状态码: {img_response.status_code}, 大小: {len(img_response.content)} 字节")
                    # 生成唯一ID
                    unique_id = uuid.uuid4()
                    date_prefix = datetime.now().strftime("%Y%m%d")
                    metadata = {'generated-by': 'sugar-pill-image-service'}
                    # 用Pillow处理三种规格
                    try:
                        image = Image.open(io.BytesIO(img_response.content)).convert("RGB")
                    except Exception as e:
                        logger.error(f"Pillow无法打开图片: {str(e)}", exc_info=True)
                        break
                    sizes = {
                        "org": (1600, 896),
                        "card": (776, 435),
                        "cover": (1600, 300)
                    }
                    for suffix, size in sizes.items():
                        img_copy = image.copy()
                        # 居中裁剪到目标比例
                        target_w, target_h = size
                        src_w, src_h = img_copy.size
                        src_ratio = src_w / src_h
                        target_ratio = target_w / target_h
                        if src_ratio > target_ratio:
                            # 原图更宽，裁掉两侧
                            new_w = int(src_h * target_ratio)
                            left = (src_w - new_w) // 2
                            right = left + new_w
                            box = (left, 0, right, src_h)
                        else:
                            # 原图更高，裁掉上下
                            new_h = int(src_w / target_ratio)
                            top = (src_h - new_h) // 2
                            bottom = top + new_h
                            box = (0, top, src_w, bottom)
                        img_cropped = img_copy.crop(box)
                        img_resized = img_cropped.resize((target_w, target_h), Image.LANCZOS)
                        buffer = io.BytesIO()
                        img_resized.save(buffer, format="PNG")
                        buffer.seek(0)
                        filename = f"{unique_id}-{suffix}.png"
                        s3_key = f"{s3_prefix}{date_prefix}/{filename}"
                        logger.info(f"上传{suffix}图片到S3: {s3_bucket}/{s3_key}")
                        try:
                            s3_client.upload_fileobj(
                                buffer,
                                s3_bucket,
                                s3_key,
                                ExtraArgs={
                                    'ContentType': 'image/png',
                                    'CacheControl': 'max-age=31536000',
                                    'Metadata': metadata,
                                    'ACL': 'public-read'
                                }
                            )
                            s3_url = f"https://{s3_bucket}.s3.{s3_region}.amazonaws.com/{s3_key}"
                            s3_urls.append(s3_url)
                            logger.info(f"{suffix}图片上传S3成功，URL: {s3_url}")
                        except Exception as e:
                            logger.error(f"上传{suffix}图片到S3失败: {str(e)}", exc_info=True)
                    break  # 成功处理一张原图后退出重试
                except Exception as e:
                    retry_count += 1
                    logger.warning(f"图片 #{i} 处理失败 (尝试 {retry_count}/{max_retries}): {str(e)}")
                    if retry_count >= max_retries:
                        logger.error(f"⚠️ 图片 #{i} 处理最终失败: {str(e)}", exc_info=True)
                    else:
                        await asyncio.sleep(1)
    logger.info(f"图片处理完成，共上传到S3 {len(s3_urls)} 张图片")
    return s3_urls

async def process_task_background(task_id: str, prompt: str):
    logger.info(f"开始后台处理任务: {task_id}, 提示词: {prompt}")
    max_wait_seconds = 300
    check_interval = 3
    start_time = time.time()
    
    # 初始化时包含task_id
    task_status[task_id] = {"task_id": task_id, "status": "PROCESSING", "image_urls": []}
    logger.info(f"任务 {task_id} 状态已初始化为 'PROCESSING'")
    
    async with httpx.AsyncClient() as client:
        while time.time() - start_time < max_wait_seconds:
            try:
                logger.info(f"查询任务 {task_id} 状态, 已等待 {int(time.time() - start_time)} 秒")
                response = await client.get(
                    QUERY_TASK_URL.format(task_id=task_id),
                    headers={"Authorization": f"Bearer {API_KEY}"}
                )
                result = response.json()
                logger.debug(f"查询结果: {json.dumps(result)}")
                
                if "output" not in result:
                    logger.error(f"无效的响应格式: {json.dumps(result)}")
                    raise ValueError("Invalid response format")
                
                status = result["output"]["task_status"]
                logger.info(f"任务 {task_id} 状态: {status}")
                
                if status == "SUCCEEDED":
                    logger.info(f"任务 {task_id} 成功完成")
                    if "results" in result["output"]:
                        results_count = len(result["output"].get("results", []))
                        logger.info(f"开始保存图片，结果数量: {results_count}")
                        image_urls = await save_images(result, prompt)
                        logger.info(f"图片保存完成，URL: {image_urls}")
                        task_status[task_id] = {"task_id": task_id, "status": "COMPLETED", "image_urls": image_urls}
                    else:
                        logger.warning(f"任务 {task_id} 成功但没有结果")
                        task_status[task_id] = {"task_id": task_id, "status": "FAILED", "error": "No results in response"}
                    return
                elif status == "FAILED":
                    error_msg = result["output"].get("error", {}).get("message", "Unknown error")
                    logger.error(f"任务 {task_id} 失败: {error_msg}")
                    task_status[task_id] = {"task_id": task_id, "status": "FAILED", "error": error_msg}
                    return
                elif status in ["PENDING", "RUNNING"]:
                    logger.info(f"任务 {task_id} 进行中: {status}，等待 {check_interval} 秒")
                    await asyncio.sleep(check_interval)
                    continue
                else:
                    logger.warning(f"任务 {task_id} 未知状态: {status}")
                    task_status[task_id] = {"task_id": task_id, "status": "FAILED", "error": f"Unknown status: {status}"}
                    return
                    
            except Exception as e:
                logger.error(f"处理任务 {task_id} 时出错: {str(e)}", exc_info=True)
                task_status[task_id] = {"task_id": task_id, "status": "FAILED", "error": str(e)}
                return
                
        logger.warning(f"任务 {task_id} 处理超时，已等待 {max_wait_seconds} 秒")
        task_status[task_id] = {"task_id": task_id, "status": "TIMEOUT", "error": "Task processing timeout"}


@app.post("/generate-image", response_model=ImageResponse)
async def generate_image(request: ImageRequest):
    """修改后的生成图片接口（使用asyncio.create_task）"""
    logger.info(f"收到完整请求: {request.dict()}")
    try:
        task_id = await create_image_task(request)
        logger.info(f"成功创建阿里云任务，任务ID: {task_id}")
        
        # 创建并跟踪后台任务
        task = asyncio.create_task(
            process_task_background(task_id, request.prompt)
        )
        app.state.task_set.add(task)
        logger.info(f"已创建后台任务，当前任务集合大小: {len(app.state.task_set)}")
        
        # 任务完成后自动清理
        task.add_done_callback(
            lambda t: app.state.task_set.discard(t)
        )
        
        return ImageResponse(
            task_id=task_id,
            status="PROCESSING",
            image_urls=[]
        )
        
    except HTTPException as e:
        logger.error(f"生成图像失败(HTTPException): {e.detail}")
        raise e
    except Exception as e:
        logger.error(f"生成图像失败(Exception): {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"生成图像失败: {str(e)}")

@app.get("/task/{task_id}", response_model=ImageResponse)
async def get_task_status(task_id: str):
    logger.info(f"获取任务状态: {task_id}")
    try:
        # 如果任务不在字典中
        if task_id not in task_status:
            logger.info(f"任务 {task_id} 不在本地状态字典中，尝试从阿里云查询")
            # 尝试从阿里云获取状态
            try:
                result = await query_task(task_id)
                status = result.get("output", {}).get("task_status", "UNKNOWN")
                logger.info(f"从阿里云获取到任务状态: {status}")
                return ImageResponse(
                    task_id=task_id,
                    status=status,
                    image_urls=[],
                    error=None
                )
            except Exception as e:
                logger.error(f"任务状态查询失败: {str(e)}")
                return ImageResponse(
                    task_id=task_id,
                    status="ERROR",
                    image_urls=[],
                    error=f"任务状态查询失败: {str(e)}"
                )
        
        logger.info(f"任务 {task_id} 在本地状态字典中: {task_status[task_id]}")
        # 直接创建ImageResponse对象
        return ImageResponse(
            task_id=task_id,
            status=task_status[task_id].get("status", "UNKNOWN"),
            image_urls=task_status[task_id].get("image_urls", []),
            error=task_status[task_id].get("error")
        )
    except Exception as e:
        logger.error(f"处理任务状态请求出错: {str(e)}")
        return ImageResponse(
            task_id=task_id,
            status="ERROR",
            image_urls=[],
            error=f"处理请求时出错: {str(e)}"
        )

@app.get("/openapi.json", include_in_schema=False)
async def get_openapi_spec():
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "bailian_image_service",
            "version": "0.1.0",
            "description": "阿里云灵积图像生成API"
        },
        "servers": [{"url": PUBLIC_URL_BASE}],
        "paths": {
            "/generate-image": {
                "post": {
                    "summary": "生成图像",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ImageRequest"}
                            }
                        },
                        "required": True
                    },
                    "responses": {
                        "200": {
                            "description": "成功",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ImageResponse"}
                                }
                            }
                        },
                        "422": {
                            "description": "验证错误",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/HTTPValidationError"}
                                }
                            }
                        }
                    }
                }
            },
            "/task/{task_id}": {
                "get": {
                    "summary": "获取任务状态",
                    "parameters": [{
                        "name": "task_id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"}
                    }],
                    "responses": {
                        "200": {
                            "description": "成功",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ImageResponse"}
                                }
                            }
                        }
                    }
                }
            }
        },
        "components": {
            "schemas": {
                "ImageRequest": {
                    "type": "object",
                    "required": ["prompt"],
                    "properties": {
                        "prompt": {"type": "string", "example": "一只可爱的猫咪"},
                        "negative_prompt": {"type": "string", "default": ""},
                        "model": {"type": "string", "default": "wanx2.1-t2i-turbo"},
                        "size": {"type": "string", "default": "1024*1024"},
                        "n": {"type": "integer", "default": 1, "minimum": 1, "maximum": 4}
                    }
                },
                "ImageResponse": {
                    "type": "object",
                    "required": ["task_id", "status"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "status": {"type": "string"},
                        "image_urls": {"type": "array", "items": {"type": "string"}},
                        "error": {"type": "string", "nullable": True}
                    }
                },
                "HTTPValidationError": {
                    "type": "object",
                    "properties": {
                        "detail": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/ValidationError"}
                        }
                    }
                },
                "ValidationError": {
                    "type": "object",
                    "required": ["loc", "msg", "type"],
                    "properties": {
                        "loc": {"type": "array", "items": {"type": "string"}},
                        "msg": {"type": "string"},
                        "type": {"type": "string"}
                    }
                }
            }
        }
    }

@app.get("/.well-known/ai-plugin.json")
async def plugin_manifest():
    return {
        "schema_version": "v1",
        "name_for_human": "阿里云AI绘图工具",
        "name_for_model": "aliyun_image_generator",
        "description_for_human": "使用阿里云灵积模型生成AI图像",
        "description_for_model": "这个工具使用阿里云灵积模型API根据文本描述生成图像。",
        "auth": {"type": "none"},
        "api": {
            "type": "openapi",
            "url": f"{PUBLIC_URL_BASE}/openapi.json"
        },
        "logo_url": f"{PUBLIC_URL_BASE}/logo.png",
        "contact_email": "your-email@example.com",
        "legal_info_url": f"{PUBLIC_URL_BASE}/legal"
    }

@app.get("/test-s3-connection")
async def test_s3_connection():
    """测试S3连接和配置"""
    try:
        # 初始化S3客户端
        s3_client = boto3.client(
            's3', 
            region_name=os.getenv("AWS_REGION", "ap-southeast-1"),
            aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
        )
        
        # 列出存储桶中的对象（最多10个）
        response = s3_client.list_objects_v2(
            Bucket=os.getenv("S3_BUCKET", "sparkle-web-static"),
            Prefix=os.getenv("S3_PREFIX", "starrybook/image/blogs/"),
            MaxKeys=10
        )
        
        # 检查响应
        if 'Contents' in response:
            object_count = len(response['Contents'])
            return {
                "status": "success",
                "message": f"成功连接到S3并列出{object_count}个对象",
                "bucket": os.getenv("S3_BUCKET", "sparkle-web-static"),
                "prefix": os.getenv("S3_PREFIX", "starrybook/image/blogs/"),
                "region": os.getenv("AWS_REGION", "ap-southeast-1")
            }
        else:
            return {
                "status": "success",
                "message": "成功连接到S3但指定前缀下没有对象",
                "bucket": os.getenv("S3_BUCKET", "sparkle-web-static"),
                "prefix": os.getenv("S3_PREFIX", "starrybook/image/blogs/"),
                "region": os.getenv("AWS_REGION", "ap-southeast-1")
            }
    except Exception as e:
        logger.error(f"测试S3连接失败: {str(e)}", exc_info=True)
        return {
            "status": "error",
            "message": f"连接S3失败: {str(e)}",
            "bucket": os.getenv("S3_BUCKET", "sparkle-web-static"),
            "prefix": os.getenv("S3_PREFIX", "starrybook/image/blogs/"),
            "region": os.getenv("AWS_REGION", "ap-southeast-1")
        }
@app.get("/test-env")
async def test_env():
    """测试环境变量加载情况"""
    return {
        "aws_key_id_exists": bool(os.getenv("AWS_ACCESS_KEY_ID")),
        "aws_secret_key_exists": bool(os.getenv("AWS_SECRET_ACCESS_KEY")),
        "aws_region": os.getenv("AWS_REGION", "未设置"),
        "s3_bucket": os.getenv("S3_BUCKET", "未设置"),
        "s3_prefix": os.getenv("S3_PREFIX", "未设置")
    }   

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)