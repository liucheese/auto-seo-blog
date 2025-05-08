# 使用 Python 3.12 作为基础镜像
FROM python:3.12-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 创建应用目录结构
RUN mkdir -p /app/dify-scheduler \
             /app/sugar-pill-image-service \
             /app/shared_data/blogs \
             /app/shared_data/logs \
             /app/shared_data/images

# 复制项目依赖文件
COPY ./dify-scheduler/requirements.txt /app/dify-scheduler/
COPY ./sugar-pill-image-service/requirements.txt /app/sugar-pill-image-service/

# 安装 Python 依赖
RUN pip install --no-cache-dir -r /app/dify-scheduler/requirements.txt
RUN pip install --no-cache-dir -r /app/sugar-pill-image-service/requirements.txt

# 复制项目代码
COPY ./dify-scheduler/ /app/dify-scheduler/
COPY ./sugar-pill-image-service/ /app/sugar-pill-image-service/

# 复制启动脚本
COPY <<EOF /app/entrypoint.sh
#!/bin/bash

# 设置环境变量
export PYTHONPATH=/app

# 根据传入的参数决定启动哪个服务
case "\$1" in
  "image-service")
    echo "Starting Image Service..."
    cd /app/sugar-pill-image-service
    uvicorn main:app --host 0.0.0.0 --port \${PORT:-8000}
    ;;
  "scheduler")
    echo "Starting Scheduler..."
    cd /app/dify-scheduler
    python trigger_dify.py
    ;;
  "scheduler-cron")
    echo "Setting up cron job for scheduler..."
    echo "0 6 * * * cd /app/dify-scheduler && python trigger_dify.py >> /app/shared_data/logs/scheduler.log 2>&1" > /etc/cron.d/scheduler
    chmod 0644 /etc/cron.d/scheduler
    crontab /etc/cron.d/scheduler
    cron -f
    ;;
  *)
    echo "Usage: \$0 {image-service|scheduler|scheduler-cron}"
    echo "Starting image service by default..."
    cd /app/sugar-pill-image-service
    uvicorn main:app --host 0.0.0.0 --port \${PORT:-8000}
    ;;
esac
EOF

# 让启动脚本可执行
RUN chmod +x /app/entrypoint.sh

# 设置环境变量
ENV PYTHONPATH=/app
ENV ENV=production

# 暴露端口
EXPOSE 8000

# 设置启动命令
ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["image-service"]