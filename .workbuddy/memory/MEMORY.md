# 项目记忆 — 周末交友战术指挥舱

## 项目架构
- 项目根：`D:\360MoveData\Users\yliua\Desktop\workbuddy-周末交友战术指挥\`
- Python 环境：`C:\Users\yliua\.workbuddy\binaries\python\envs\default\Scripts\python.exe`
- 数据管线：`src/main.py` → `output/week_schedule.json` → `index.html`
- 本地预览：`python -m http.server 8080`（项目根目录）

## 当前版本：v5.1 无损还原 + 自动部署
- A区(左50%)：Master-Detail布局 — 全局模式100%Grid / 分日模式50%Detail+50%List
- B区(右上50%)：Leaflet 地图 + 编号图钉/热力圈
- C区(右下50%)：上66.6% C4面板(90%冲突对比+10%气象限行) + 下33.3%甘特图 C1/C2/C3
- 三区通过 `global_id` 联动：点击/悬停跨区高亮+flyTo+A区Detail联动+C4冲突面板联动
- 气象限行：JavaScript 按日 Mock 数据（周五限行4和9、周末不限行）
- A区Detail面板：`full_raw_text` 无损全文渲染 + `images` 图片画廊
- 自动部署：`auto_deploy.bat` → 管线→Git推送→关机
- GitHub仓库：`https://github.com/independent-all/workbuddy--.git`

## 数据 Schema
- 输出格式：`week_schedule.json` (Schema v1.0)
- 活动含 `global_id` 字段（整数，1-N，全周按时间排序）
- 色调标签：cold(严肃)/warm(轻松)/neutral(中性)
- 冲突组：同日时间重叠的活动自动分组

## 关键约定
- 原点：北京市朝阳区南平里 (39.98, 116.48)
- 用户以简体中文沟通，偏好简短高效风格
- 三区筛选同步，互不独立
- GitHub Pages 开启方式：仓库 Settings → Pages → Source 选 `main` 分支 → Save
