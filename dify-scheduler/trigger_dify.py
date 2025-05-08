import json
import httpx
import asyncio
import logging
import os
import time
import random
import uuid
import re
import boto3
from botocore.exceptions import ClientError
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename=os.path.join(os.path.dirname(__file__), 'dify_trigger.log')
)
logger = logging.getLogger(__name__)

# Get configuration from environment variables
DIFY_API_KEY = os.getenv("DIFY_API_KEY")
DIFY_WORKFLOW_API_URL = os.getenv("DIFY_WORKFLOW_API_URL")
BLOG_STORAGE_PATH = os.getenv("BLOG_STORAGE_PATH", "./blogs")
IMAGE_SERVICE_URL = os.getenv("IMAGE_SERVICE_URL", "http://localhost:8000")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-1")
DYNAMODB_TABLE_NAME = os.getenv("DYNAMODB_TABLE_NAME", "starry_book_blog")

# Authors and colors list
AUTHORS = ["Whit", "LunaGaze", "Daisy", "Lily", "Emma", "Joy", "Mia", "AvaStar", "Maya", "Emily"]
COLORS = ["#A8A0F9", "#D7A0F9", "#FFE2EB", "#FFE4C1", "#DAFFF6", "#FFFD92"]

# Validate necessary environment variables
if not DIFY_API_KEY:
    raise ValueError("DIFY_API_KEY environment variable must be set")
if not DIFY_WORKFLOW_API_URL:
    raise ValueError("DIFY_WORKFLOW_API_URL environment variable must be set")
if not DYNAMODB_TABLE_NAME:
    logger.warning("Warning: DYNAMODB_TABLE_NAME not set, using default 'starry_book_blog'")

# Create blog storage directory
os.makedirs(BLOG_STORAGE_PATH, exist_ok=True)
logger.info(f"Ensuring blog storage directory exists: {BLOG_STORAGE_PATH}")

def generate_slug(title):
    """Generate URL-friendly slug from title"""
    # Convert title to lowercase
    slug = title.lower()
    # Remove special characters, replace spaces and other non-alphanumeric characters with hyphens
    slug = re.sub(r'[^a-z0-9]+', '-', slug)
    # Remove hyphens from beginning and end
    slug = slug.strip('-')
    return slug

def save_blog(content, idx):
    """Save blog content to Markdown file"""
    date_str = datetime.now().strftime("%Y-%m-%d")
    dir_path = os.path.join(BLOG_STORAGE_PATH, date_str)
    os.makedirs(dir_path, exist_ok=True)
    file_path = os.path.join(dir_path, f"blog_{idx}.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info(f"Blog saved: {file_path}")

async def get_image_urls(task_id):
    """Get image generation service results via task_id"""
    logger.info(f"Starting to query image task status: {task_id}")
    max_attempts = 30  # Maximum 30 attempts
    wait_time = 5  # Wait 5 seconds between attempts
    
    async with httpx.AsyncClient(timeout=30.0, verify=False) as client:
        for attempt in range(max_attempts):
            try:
                response = await client.get(f"{IMAGE_SERVICE_URL}/task/{task_id}")
                response.raise_for_status()
                result = response.json()
                
                logger.info(f"Image task status query result: {result}")
                
                # If task is complete and has image URLs
                if result.get("status") == "COMPLETED" and result.get("image_urls"):
                    logger.info(f"Image generation successful, got URLs: {result['image_urls']}")
                    return result['image_urls']
                
                # If task failed
                if result.get("status") in ["FAILED", "ERROR", "TIMEOUT"]:
                    logger.error(f"Image task failed: {result.get('error', 'Unknown error')}")
                    return []
                
                logger.info(f"Image task in progress ({attempt+1}/{max_attempts}), waiting {wait_time} seconds...")
                await asyncio.sleep(wait_time)
            except Exception as e:
                logger.error(f"Error querying image task status: {str(e)}")
                await asyncio.sleep(wait_time)
    
    logger.warning(f"Waiting for image generation timed out, task ID: {task_id}")
    return []

def get_image_urls_by_type(image_urls, unique_id):
    """Organize image_urls into card, cover, org format"""
    card_url = cover_url = org_url = ""
    
    # If all URLs already exist, filter by -suffix
    if image_urls:
        for url in image_urls:
            if "-card" in url:
                card_url = url
            elif "-cover" in url:
                cover_url = url
            elif "-org" in url:
                org_url = url
    
    # If no URLs found, construct S3-based URL template
    if not card_url and not cover_url and not org_url and unique_id:
        date_prefix = datetime.now().strftime("%Y%m%d")
        s3_prefix = "starrybook/image/blogs"
        s3_bucket = "sparkle-web-static"
        s3_region = "ap-southeast-1"
        
        base_url = f"https://{s3_bucket}.s3.{s3_region}.amazonaws.com/{s3_prefix}/{date_prefix}/{unique_id}"
        card_url = f"{base_url}-card.png"
        cover_url = f"{base_url}-cover.png"
        org_url = f"{base_url}-org.png"
    
    return card_url, cover_url, org_url

def save_blog_to_db(blog_data, image_urls):
    """Save blog content and image URLs to DynamoDB"""
    try:
        # Parse blog data
        blog_text = json.loads(blog_data.get("text", "{}"))
        
        # Extract required fields
        title = blog_text.get("title", "")
        content = blog_text.get("article", "")  # Note: API returns 'article', not 'content'
        tag = blog_text.get("tag", "")
        keywords = blog_text.get("keywords", "")
        description = blog_text.get("description", "")
        
        # Extract main keyword from keywords
        keyword = keywords.split(',')[0].strip() if keywords else ""
        
        # Generate other necessary fields
        blog_uid = str(uuid.uuid4())
        author = random.choice(AUTHORS)
        avatar = f"https://sparkle-web-static.s3.ap-southeast-1.amazonaws.com/starrybook/image/blog-authors/{author}.webp"
        color = random.choice(COLORS)
        created_at = updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        published = True
        slug = generate_slug(title)
        
        # Get image URLs for all three types
        card_url, cover_url, org_url = get_image_urls_by_type(image_urls, blog_uid)
        
        # Create a DynamoDB client
        try:
            # Connect to DynamoDB
            dynamodb = boto3.resource('dynamodb', region_name=AWS_REGION)
            table = dynamodb.Table(DYNAMODB_TABLE_NAME)
            
            # Prepare item data for DynamoDB
            item = {
                'uid': blog_uid,
                'author': author,
                'avatar': avatar,
                'card': card_url,
                'color': color,
                'content': content,
                'cover': cover_url,
                'created_at': created_at,
                'description': description,
                'keyword': keyword,
                'keywords': keywords,
                'org': org_url,
                'published': published,
                'slug': slug,
                'tag': tag,
                'title': title,
                'updated_at': updated_at
            }
            
            # Put item into DynamoDB table
            max_retries = 3
            retry_delay = 2
            
            for attempt in range(max_retries):
                try:
                    response = table.put_item(Item=item)
                    logger.info(f"Successfully saved blog '{title}' to DynamoDB, ID: {blog_uid}")
                    logger.info(f"DynamoDB response: {response}")
                    return True
                except ClientError as e:
                    error_code = e.response['Error']['Code']
                    error_message = e.response['Error']['Message']
                    
                    if attempt < max_retries - 1:
                        logger.warning(f"DynamoDB put_item failed (attempt {attempt+1}/{max_retries}), error: {error_code} - {error_message}, retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                    else:
                        logger.error(f"DynamoDB put_item failed, max retries ({max_retries}) reached, error: {error_code} - {error_message}")
                        # Log blog info for manual processing
                        logger.info(f"Blog that couldn't be saved - Title: {title}, ID: {blog_uid}")
                        logger.info(f"Card URL: {card_url}")
                        logger.info(f"Cover URL: {cover_url}")
                        logger.info(f"Original URL: {org_url}")
                        # Still return True to allow the process to continue
                        return True
            
        except Exception as e:
            logger.error(f"Error setting up DynamoDB connection: {str(e)}", exc_info=True)
            # Log blog info for manual processing
            logger.info(f"Blog that couldn't be saved - Title: {title}, ID: {blog_uid}")
            logger.info(f"Card URL: {card_url}")
            logger.info(f"Cover URL: {cover_url}")
            logger.info(f"Original URL: {org_url}")
            # Return True to allow the process to continue
            return True
            
    except Exception as e:
        logger.error(f"Failed to save blog to DynamoDB: {str(e)}", exc_info=True)
        # Return True to allow the process to continue
        return True

async def trigger_dify_workflow():
    """Trigger a Dify workflow and wait for results"""
    # Prepare request content
    payload = {
        "inputs": {},  
        "files": [],   
        "response_mode": "blocking",  
        "user": "auto-scheduler" 
    }
    
    logger.info(f"Sending request to: {DIFY_WORKFLOW_API_URL}")
    logger.debug(f"Request body: {json.dumps(payload, ensure_ascii=False)}")
    
    async with httpx.AsyncClient(
        verify=False,  # Disable certificate verification
        timeout=120.0,
        limits=httpx.Limits(max_keepalive_connections=1)
    ) as client:
        logger.info(f"Requesting URL: {DIFY_WORKFLOW_API_URL}")

        try:
            # Force override hostname validation (for known httpx issue)
            response = await client.post(
                DIFY_WORKFLOW_API_URL,
                headers={
                    "Authorization": f"Bearer {DIFY_API_KEY}",
                    "Content-Type": "application/json",
                    # Explicitly declare accepting insecure connections
                    "X-Forwarded-Proto": "https"  
                },
                json=payload,
                # Disable default hostname validation behavior
                extensions={"force_https": False}  
            )
            
            logger.info(f"Response status code: {response.status_code}")
            logger.debug(f"Response content: {response.text[:200]}...")
            
            response.raise_for_status()
            result = response.json()
            
            if "workflow_run_id" in result:
                return {
                    "workflow_run_id": result.get("workflow_run_id", ""),
                    "status": result.get("data", {}).get("status", ""),
                    "outputs": result.get("data", {}).get("outputs", {}),
                    "elapsed_time": result.get("data", {}).get("elapsed_time", 0)
                }
            else:
                logger.warning(f"Unknown response format: {result}")
                return None
            
        except httpx.HTTPStatusError as e:
            logger.error(f"Dify API returned error: {e.response.text}")
            return None
        except Exception as e:
            logger.error(f"Request failed: {str(e)}")
            return None

async def process_single_blog():
    """Process complete workflow for a single blog"""
    try:
        # Step 1: Trigger Dify to generate blog content
        dify_result = await trigger_dify_workflow()
        if not dify_result or not dify_result.get("outputs"):
            logger.error("Dify API returned empty result")
            return False
        
        outputs = dify_result.get("outputs", {})
        logger.info(f"Dify returned result: {json.dumps(outputs, ensure_ascii=False)[:200]}...")
        
        # Step 2: Get image task ID
        # 修改后的代码
        image_data = outputs.get("image")
        try:
            if image_data is None:
                logger.warning("未找到图片任务数据")
                image_urls = []
                image_task_id = None
            else:
                # 检查image_data是否为JSON字符串
                if isinstance(image_data, str):
                    try:
                        # 尝试解析为JSON
                        image_info = json.loads(image_data)
                        # 如果是JSON对象，尝试获取task_id字段
                        if isinstance(image_info, dict) and "task_id" in image_info:
                            image_task_id = image_info["task_id"]
                        else:
                            # 如果不是包含task_id的JSON对象，直接使用字符串作为任务ID
                            image_task_id = image_data
                    except json.JSONDecodeError:
                        # 不是JSON，直接使用字符串作为任务ID
                        image_task_id = image_data
                else:
                    # 如果不是字符串，记录并跳过
                    logger.warning(f"图片任务数据类型非预期: {type(image_data)}")
                    image_urls = []
                    image_task_id = None
                
                if image_task_id:
                    logger.info(f"获取到图片任务ID: {image_task_id}")
                    # 步骤3: 查询图片URL并获取三种规格
                    image_urls = await get_image_urls(image_task_id)
                else:
                    logger.warning("未能提取有效的图片任务ID")
                    image_urls = []
        except Exception as e:
            logger.error(f"处理图片任务信息时出错: {str(e)}", exc_info=True)
            image_urls = []
        
        # Step 4: Save blog content to database
        saved = save_blog_to_db(outputs, image_urls)
        
        # Also save to local file (optional)
        if "text" in outputs:
            try:
                text_data = json.loads(outputs["text"])
                content = text_data.get("article", "")
                if content:
                    save_blog(content, datetime.now().strftime("%Y%m%d_%H%M%S"))
            except json.JSONDecodeError:
                logger.error(f"Failed to parse blog content: {outputs.get('text')}")
        
        return saved
    except Exception as e:
        logger.error(f"Error in blog processing workflow: {str(e)}", exc_info=True)
        return False

async def main():
    """Main function: Batch process blogs"""
    success_count = 0
    total_count = 100
    
    for i in range(total_count):
        try:
            logger.info(f"Starting to process blog {i+1}/{total_count}")
            success = await process_single_blog()
            
            if success:
                success_count += 1
                logger.info(f"Successfully processed blog {i+1}/{total_count}")
            else:
                logger.warning(f"Failed to process blog {i+1}/{total_count}")
            
            # Add random delay to avoid API rate limiting
            await asyncio.sleep(random.uniform(1.0, 3.0))
        except Exception as e:
            logger.error(f"Error processing blog {i+1}/{total_count}: {str(e)}", exc_info=True)
    
    logger.info(f"Batch processing complete: Success {success_count}/{total_count}")

if __name__ == "__main__":
    start_time = time.time()
    asyncio.run(main())
    elapsed = time.time() - start_time
    logger.info(f"Total execution time: {elapsed:.2f} seconds")