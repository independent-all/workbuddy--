"""
周末交友战术指挥舱 — IMA 真实数据接入脚本 (Phase 4)
=====================================================

功能：从本地 .txt / .md 文件批量导入相亲活动笔记，自动提取
Schema v1.0 所需字段，输出 parsed_events.json 供下游管线使用。

支持两种笔记格式：
  格式 A（推荐）— YAML 前置元数据 + Markdown 正文
  格式 B（兜底）— 纯文本正则提取

输出：data/ima_parsed_events.json（与 sample_data.SAMPLE_EVENTS 同构）
"""

import json
import re
import os
from pathlib import Path
from datetime import datetime
from typing import Optional


# ============================================================
# 配置区
# ============================================================

# 笔记目录（按你实际的目录修改）
NOTES_DIR = "ima_notes"

# 输出路径
OUTPUT_PATH = "data/ima_parsed_events.json"

# 周次标签（用于自动填充日期）
DEFAULT_WEEK_TAG = "2026-W21"

# 周次 → 日期映射（可按需扩展）
WEEK_DATE_MAP = {
    "2026-W21": {
        "monday":    "2026-05-18",
        "tuesday":   "2026-05-19",
        "wednesday": "2026-05-20",
        "thursday":  "2026-05-21",
        "friday":    "2026-05-22",
        "saturday":  "2026-05-23",
        "sunday":    "2026-05-24",
    }
}


# ============================================================
# 字段提取器
# ============================================================

def extract_yaml_frontmatter(text: str) -> Optional[dict]:
    """
    提取 YAML 风格前置元数据（格式 A）。
    支持 --- 分隔的 key: value 对。
    """
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if not m:
        return None

    fm = {}
    body = text[m.end():]
    for line in m.group(1).split('\n'):
        line = line.strip()
        if ':' in line:
            key, _, value = line.partition(':')
            key = key.strip()
            value = value.strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1]
            elif value.startswith("'") and value.endswith("'"):
                value = value[1:-1]
            if value:
                fm[key] = value
    fm['_body'] = body.strip()
    return fm


def extract_date(text: str, filename: str = "") -> Optional[str]:
    """从文本或文件名中提取日期 YYYY-MM-DD"""
    # 标准格式
    m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
    if m:
        return m.group(1)
    # 中文格式：5月22日
    m = re.search(r'(\d{1,2})月(\d{1,2})[日号]', text)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        return f"2026-{month:02d}-{day:02d}"
    # 从文件名提取
    m = re.search(r'(\d{4}-\d{2}-\d{2})', filename)
    if m:
        return m.group(1)
    return None


def extract_day_of_week(text: str, filename: str = "") -> Optional[str]:
    """从文本中提取星期几 → 'friday'|'saturday'|'sunday'"""
    day_map = {
        '周五': 'friday', '星期五': 'friday',
        '周六': 'saturday', '星期六': 'saturday',
        '周日': 'sunday', '星期天': 'sunday', '星期日': 'sunday',
        '周一': 'monday', '周二': 'tuesday', '周三': 'wednesday', '周四': 'thursday',
    }
    for cn, en in day_map.items():
        if cn in text or cn in filename:
            return en
    return None


def extract_time_range(text: str) -> Optional[str]:
    """提取时间范围 → '14:00-17:00'"""
    # 标准格式 14:00-17:00
    m = re.search(r'(\d{1,2}:\d{2})\s*[-~至到]\s*(\d{1,2}:\d{2})', text)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    # 中文格式：下午2点到5点
    m = re.search(r'(上午|下午|晚上)(\d{1,2})点[到至]\s*(上午|下午|晚上)?(\d{1,2})点', text)
    if m:
        def to_24h(ampm, h):
            h = int(h)
            if ampm == '下午' or ampm == '晚上':
                return h + 12 if h < 12 else h
            return h
        start = to_24h(m.group(1), m.group(2))
        end_ampm = m.group(3) or m.group(1)
        end = to_24h(end_ampm, m.group(4))
        return f"{start:02d}:00-{end:02d}:00"
    return None


def extract_fee(text: str) -> Optional[int]:
    """提取费用（元）"""
    # ¥198 / 198元/人 / 费用：198
    for pat in [
        r'[¥￥]\s*(\d+)',
        r'(\d+)\s*元\s*/\s*人',
        r'费用[：:]\s*(\d+)',
        r'(\d+)\s*元\s*[／/]\s*位',
    ]:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))
    return None


def extract_participants(text: str) -> Optional[int]:
    """提取参与人数"""
    for pat in [
        r'限\s*(\d+)\s*人', r'(\d+)\s*人[数参]',
        r'(\d+)[－-]\s*(\d+)\s*人',  # 取上限
        r'人数[：:]\s*(\d+)',
    ]:
        m = re.search(pat, text)
        if m:
            if m.lastindex and m.lastindex >= 2:
                return int(m.group(2))
            return int(m.group(1))
    return None


def extract_address(text: str) -> Optional[str]:
    """提取北京地址"""
    # 匹配北京区域+具体地址
    m = re.search(
        r'(?:地址|地点|位置)[：:]\s*(.{5,40}?)(?:\n|$|。|，)',
        text
    )
    if m:
        return m.group(1).strip()
    # 直接匹配北京地址模式
    m = re.search(
        r'(?:北京|朝阳|海淀|西城|东城|丰台|昌平|通州|大兴|石景山|顺义)'
        r'[区县].{2,30}?(?:座|层|楼|号|附近|周边|大厦|中心|广场|城|馆)',
        text
    )
    if m:
        return m.group(0).strip()
    return None


def extract_gender_ratio(text: str) -> Optional[str]:
    """提取男女比例"""
    patterns = [
        r'(\d+男\d+女[^，。\n]*)',
        r'男女\s*[:：]\s*(\d+[:：]\d+[^，。\n]*)',
        r'男女比例[：:]\s*(.{4,20}?)(?:。|\n|，)',
        r'(男女不限|男女均衡|男女1:1.*?(?:\)|）|$|\n))',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return None


def extract_threshold(text: str) -> Optional[str]:
    """提取参与门槛"""
    patterns = [
        r'(?:门槛|要求|限)[：:]\s*(.{4,40}?)(?:。|\n|$)',
        r'(?:需|要求|限)(.{4,30}?(?:学历|收入|年[薪收]|工作|行业|着装).{0,20}?)(?:。|，|\n|$)',
        r'参与者[多为需].{4,40}?(?:。|\n)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            result = m.group(1).strip()
            if len(result) >= 3:
                return result
    # 无门槛标记
    if re.search(r'(无门槛|不限|新手友好|零基础|无学历|欢迎所有)', text):
        return "无门槛"
    return None


def extract_core_activities(text: str) -> Optional[str]:
    """提取核心互动环节"""
    patterns = [
        r'(?:核心环节|环节|流程|活动内容)[：:]\s*(.{4,80}?)(?:。\s*\n|\n\n|\n|$)',
        r'(?:含|设置|包括)(.{4,60}?(?:环节|交流|分享|配对|游戏|破冰).{0,30}?)(?:。|，|\n)',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return None


def extract_title(text: str, filename: str = "") -> str:
    """提取活动标题"""
    # YAML frontmatter 优先
    fm = extract_yaml_frontmatter(text)
    if fm and fm.get('title'):
        return fm['title']
    # 正文第一行
    first_line = text.strip().split('\n')[0]
    if first_line and not first_line.startswith('#') and len(first_line) <= 50:
        return first_line.strip()
    # Markdown 标题
    m = re.search(r'^#\s+(.{2,40})$', text, re.MULTILINE)
    if m:
        return m.group(1).strip()
    # 文件名（去掉序号前缀）
    name = Path(filename).stem
    name = re.sub(r'^[\d\-\s_]+', '', name)
    return name or "未命名活动"


def extract_organizer(text: str) -> str:
    """提取主办方"""
    for pat in [
        r'主办[方单位][：:]\s*(.{2,20}?)(?:\n|，|。)',
        r'组织者[：:]\s*(.{2,20}?)(?:\n|，|。)',
    ]:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return "未知主办方"


def extract_ima_url(text: str) -> Optional[str]:
    """提取 IMA 原始链接"""
    m = re.search(r'https?://ima\.qq\.com/\S+', text)
    if m:
        return m.group(0).rstrip(')）')
    # 通用 URL
    m = re.search(r'(?:链接|来源|原文|ima)[：:]\s*(https?://\S+)', text)
    if m:
        return m.group(1).rstrip(')）')
    return None


def extract_all_images(text: str) -> list:
    """
    从文本中提取所有图片链接，返回去重后的图片 URL 列表。

    支持格式：
      - Markdown: ![alt](url)
      - HTML: <img src="url">
    """
    images = []
    seen = set()

    # Markdown 图片语法 ![alt](url)
    for m in re.finditer(r'!\[.*?\]\((https?://\S+?)\)', text):
        url = m.group(1).rstrip(')）')
        if url not in seen:
            seen.add(url)
            images.append(url)

    # HTML <img src="url"> 或 <img src='url'>
    for m in re.finditer(r'<img\s+[^>]*src\s*=\s*["\'](https?://[^"\']+)["\']', text, re.IGNORECASE):
        url = m.group(1).rstrip(')）')
        if url not in seen:
            seen.add(url)
            images.append(url)

    return images


def extract_cover_image(text: str) -> Optional[str]:
    """提取封面图 URL"""
    # YAML frontmatter 优先
    fm = extract_yaml_frontmatter(text)
    if fm and fm.get('cover_image'):
        return fm['cover_image']
    # Markdown 图片语法
    m = re.search(r'!\[.*?\]\((https?://\S+\.(?:jpg|jpeg|png|webp|gif)\b\S*)\)', text)
    if m:
        return m.group(1)
    # 直接 URL 模式
    m = re.search(r'(?:封面|cover|banner|header)[：:图]?\s*(https?://\S+)', text, re.IGNORECASE)
    if m:
        return m.group(1).rstrip(')）')
    return None


def extract_full_raw_text(text: str) -> str:
    """
    提取正文全文（不截断），去除 YAML 前置元数据后返回完整内容。
    保留所有换行符，供前端 detail 面板完整渲染。
    """
    # 去掉 YAML 前置
    body = re.sub(r'^---\s*\n.*?\n---\s*\n', '', text, flags=re.DOTALL)
    return body.strip()


def extract_essence_text(text: str, max_len: int = 200) -> str:
    """提取正文精华（摘要版，供海报卡片使用）"""
    body = extract_full_raw_text(text)
    if len(body) <= max_len:
        return body
    # 截取并在句号处截断
    truncated = body[:max_len + 20]
    last_period = max(truncated.rfind('。'), truncated.rfind('；'), truncated.rfind('\n'))
    if last_period > 30:
        return truncated[:last_period + 1]
    return truncated[:max_len] + '…'


# ============================================================
# 主解析函数
# ============================================================

def parse_note_file(filepath: str, week_tag: str = DEFAULT_WEEK_TAG) -> Optional[dict]:
    """
    解析单个笔记文件 → 标准活动 dict。

    Args:
        filepath: 笔记文件路径 (.txt / .md)
        week_tag: 周次标签（用于补全日期）

    Returns:
        dict | None: 标准活动记录，解析失败返回 None
    """
    path = Path(filepath)
    if not path.exists():
        print(f"  [SKIP] 文件不存在: {filepath}")
        return None

    try:
        text = path.read_text(encoding='utf-8')
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding='gbk')
        except Exception:
            print(f"  [SKIP] 编码错误: {filepath}")
            return None

    if not text.strip():
        print(f"  [SKIP] 空文件: {filepath}")
        return None

    filename = path.name

    # ── 格式 A：YAML 前置元数据 ──
    fm = extract_yaml_frontmatter(text)
    if fm:
        title = fm.get('title', extract_title(text, filename))
        organizer = fm.get('organizer', '未知主办方')
        date_str = fm.get('date', '')
        time_range = fm.get('time', '')
        fee = int(fm['fee']) if fm.get('fee') and fm['fee'].isdigit() else None
        participants = int(fm['participants']) if fm.get('participants') and fm['participants'].isdigit() else None
        address = fm.get('address', '')
        gender_ratio = fm.get('gender_ratio', '')
        threshold = fm.get('threshold', '')
        core_activities = fm.get('core_activities', '')
        ima_url = fm.get('ima_url', '')
        cover_image = fm.get('cover_image', '') or extract_cover_image(text)
        body_text = fm.get('_body', text)
    else:
        # ── 格式 B：纯文本正则提取 ──
        title = extract_title(text, filename)
        organizer = extract_organizer(text)
        date_str = extract_date(text, filename) or ''
        time_range = extract_time_range(text) or ''
        fee = extract_fee(text)
        participants = extract_participants(text)
        address = extract_address(text) or ''
        gender_ratio = extract_gender_ratio(text) or ''
        threshold = extract_threshold(text) or ''
        core_activities = extract_core_activities(text) or ''
        ima_url = extract_ima_url(text) or ''
        cover_image = extract_cover_image(text) or ''
        body_text = text

    # ── 日期补全逻辑 ──
    if not date_str:
        day_of_week = extract_day_of_week(text, filename)
        if day_of_week and week_tag in WEEK_DATE_MAP:
            date_str = WEEK_DATE_MAP[week_tag].get(day_of_week, '')
        if not date_str:
            # 从目录名推断
            for part in path.parts:
                m = re.match(r'(\d{4}-\d{2}-\d{2})', part)
                if m:
                    date_str = m.group(1)
                    break

    if not date_str:
        print(f"  [WARN] 无法确定日期: {filename}，跳过")
        return None

    # ── 时间补全 ──
    if not time_range:
        time_range = "14:00-17:00"  # 默认下午场

    # ── 无损提取：全文 + 图片 ──
    full_raw = extract_full_raw_text(text)
    all_images = extract_all_images(text)

    # ── 组装标准记录 ──
    event = {
        "title": title,
        "organizer": organizer,
        "date_str": date_str,
        "time_range": time_range,
        "fee": fee or 0,
        "participants": participants or 20,
        "address": address or "北京市朝阳区",
        "raw_text": full_raw,                       # 全文（不再截断）
        "full_raw_text": full_raw,                  # 无损全文（显式字段）
        "images": all_images,                       # 所有图片链接
        "ima_url": ima_url or f"https://ima.qq.com/kb/note/{filename}",
        "gender_ratio": gender_ratio,
        "threshold": threshold,
        "core_activities": core_activities,
        "cover_image": cover_image or "",
    }

    return event


def scan_and_import(notes_dir: str = NOTES_DIR, week_tag: str = DEFAULT_WEEK_TAG) -> list:
    """
    扫描目录下所有 .txt / .md 文件，批量解析并返回活动列表。

    Args:
        notes_dir: 笔记目录路径
        week_tag: 周次标签

    Returns:
        list[dict]: 标准活动记录列表
    """
    dir_path = Path(notes_dir)
    if not dir_path.exists():
        print(f"[ERROR] 目录不存在: {dir_path.absolute()}")
        print(f"  请创建该目录并放入 .md 或 .txt 笔记文件。")
        return []

    files = []
    for ext in ('*.md', '*.txt'):
        files.extend(dir_path.rglob(ext))

    if not files:
        print(f"[WARN] 在 {dir_path.absolute()} 中未找到任何 .md / .txt 文件")
        return []

    print(f"📂 发现 {len(files)} 个笔记文件")
    print("=" * 60)

    events = []
    for f in sorted(files):
        rel_path = f.relative_to(dir_path)
        print(f"\n📄 {rel_path}")
        event = parse_note_file(str(f), week_tag)
        if event:
            print(f"   ✅ {event['title']} | {event['date_str']} {event['time_range']}")
            events.append(event)
        else:
            print(f"   ❌ 解析失败")

    return events


def save_events(events: list, output_path: str = OUTPUT_PATH) -> str:
    """保存解析结果"""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(events, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 已保存 {len(events)} 条活动 → {path.absolute()}")
    return str(path)


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    notes_dir = sys.argv[1] if len(sys.argv) > 1 else NOTES_DIR
    week_tag = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_WEEK_TAG

    print("╔══════════════════════════════════════════════════════╗")
    print("║     IMA 真实数据接入脚本 v1.0                        ║")
    print("╚══════════════════════════════════════════════════════╝")
    print(f"  笔记目录: {Path(notes_dir).absolute()}")
    print(f"  周次标签: {week_tag}")

    events = scan_and_import(notes_dir, week_tag)

    if events:
        save_events(events)

        # 统计
        cold_kw = ['国企','央企','海归','金融','高学历','硕士','博士','年薪','体制内','事业单位','公务员','精英']
        warm_kw = ['桌游','剧本杀','户外','轰趴','夜跑','露营','飞盘','骑行','徒步','K歌','调酒','烧烤']
        cold = sum(1 for e in events if any(kw in e['raw_text'] for kw in cold_kw))
        warm = sum(1 for e in events if any(kw in e['raw_text'] for kw in warm_kw))

        print(f"\n📊 导入统计:")
        print(f"   总活动: {len(events)} 场")
        print(f"   严肃局(冷): ~{cold} 场 | 轻松局(暖): ~{warm} 场")
        print(f"   总预算: ¥{sum(e['fee'] for e in events)}")
        print(f"   总人流量: {sum(e['participants'] for e in events)} 人")

        # 提示下一步
        print(f"\n💡 下一步:")
        print(f"   1. 将 data/ima_parsed_events.json 替换 src/sample_data.py 中的 SAMPLE_EVENTS")
        print(f"   2. 运行 python src/main.py 生成真正的 week_schedule.json")
        print(f"   3. 打开 index.html 查看指挥舱")
    else:
        print("\n⚠️  未导入任何活动。请检查笔记目录和文件格式。")
