"""
周末交友战术指挥舱 — 全局配置
高德地图 API Key 在此统一管理（可设为环境变量 AMAP_KEY 覆盖）
"""

import os

# ── 原点 ──
ORIGIN_ADDRESS = "北京市朝阳区南平里"
ORIGIN_LAT = 39.9800
ORIGIN_LNG = 116.4800

# ── 高德 API ──
AMAP_KEY = os.environ.get("AMAP_KEY", "YOUR_AMAP_KEY_HERE")

# 高德地理编码 API（地址 → 经纬度）
GEOCODE_URL = "https://restapi.amap.com/v3/geocode/geo"
# 高德驾车路径规划 API（两点间距离+耗时）
DRIVING_URL = "https://restapi.amap.com/v3/direction/driving"

# ── API 超时 ──
REQUEST_TIMEOUT = 10  # 秒

# ── 色调关键词库 ──
COLD_KEYWORDS = [
    "国企", "央企", "海归", "金融", "高学历", "硕士", "博士",
    "年薪", "体制内", "事业单位", "公务员", "高净值", "精英",
    "名校", "律师", "医生", "高管", "创业者", "董事"
]

WARM_KEYWORDS = [
    "桌游", "剧本杀", "户外", "轰趴", "夜跑", "露营", "飞盘",
    "骑行", "徒步", "K歌", "调酒", "桌球", "密室", "卡丁车",
    "滑雪", "冲浪", "攀岩", "蹦床", "舞蹈", "瑜伽", "脱口秀",
    "livehouse", "市集", "野餐", "烧烤", "音乐节", "派对"
]

# ── 日期标签映射 ──
DAY_LABEL_MAP = {
    "friday":   "周五",
    "saturday": "周六",
    "sunday":   "周日",
}

# ── 路径 ──
DATA_DIR = "data"
OUTPUT_DIR = "output"
