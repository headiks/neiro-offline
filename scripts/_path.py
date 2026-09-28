"""Подключает backend/ к sys.path: скрипты импортируют модули приложения напрямую
(import planner, import db). Использование — первой строкой скрипта: import _path  # noqa"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.chdir(ROOT)   # относительные пути data/... — от корня проекта, как у сайта
