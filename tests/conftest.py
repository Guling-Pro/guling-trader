"""Pytest 配置"""
import sys
from pathlib import Path

# 加 src 到 path
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))
