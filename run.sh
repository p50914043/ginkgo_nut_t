#!/bin/bash
# 趋势识别工具（多头/空头/震荡）启动脚本
cd "$(dirname "$0")"
export PYTHONPATH=./libs:
python3 app.py
