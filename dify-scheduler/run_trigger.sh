#!/bin/bash
cd "$(dirname "$0")/.."  # 切换到项目根目录
source shared_venv/bin/activate
python dify-scheduler/trigger_dify.py