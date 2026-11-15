# 岸站 AIS 可信航迹（离线分析）

FastAPI + SQLite 网页应用：海事数据员上传两处岸站收取的 AIS 定位 CSV 原档，
系统按船生成可信航迹、隔离不可信电文，并支持下载可信航迹与对应原档。
原档与判定**同一批次同时发布**；并发上传、写盘失败或服务重启后，页面与下载
只会看到完整版本，失败批次不会覆盖此前可用的航迹。

另提供**每日船舶活动密度**发布：按 UTC 日对当天**可信 AIS 点**做 0.1° 固定网格、
按不同 MMSI 计数（同船同格只计一次），不足 3 船的格子整体隐藏，页面与 CSV 均
不含任何船号或单船航迹；首次发布即固化，重复发布与后到电文不改动旧档。

## 启动

### Docker Compose（推荐）

```bash
docker compose up --build
# 浏览器打开 http://127.0.0.1:8000
```

数据保存在命名卷 `ais-data` 中（`/data/ais.sqlite3` 与 `/data/originals/`）。
`./samples` 以只读方式挂载进容器为 `/samples`，便于在容器内直接取样例文件：

```bash
# 宿主机上传样例（两个批次，验证重投、冲突、迟到点）
curl -F 'file=@samples/batch1_base.csv' http://127.0.0.1:8000/upload
curl -F 'file=@samples/batch2_late_arrivals.csv' http://127.0.0.1:8000/upload
```

停止并保留数据：`docker compose down`；连同卷一起删除：`docker compose down -v`。

> 若本机没有 Docker（本开发环境就没有），可直接用 venv 运行，见下节；
> 两种方式使用完全相同的代码、环境变量与数据布局。

### 本地 venv

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt      # 运行
pip install -r requirements-dev.txt  # 另加测试依赖
uvicorn app.main:app --reload --port 8000
```

环境变量：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AIS_DATA_DIR` | `./data` | SQLite、`originals/` 与 `grids/` 根目录 |
| `AIS_MAX_UPLOAD_BYTES` | 67108864 (64 MiB) | 单次上传上限，超限直接拒绝且不改动航迹 |
| `AIS_PORT` | 8000 | 仅 Compose 使用的宿主端口 |
| `AIS_REPLICA_PORT` | 8001 | `ha` profile 下第二实例的宿主端口 |

## 输入格式

CSV 至少包含五列（表头大小写/别名均可，支持 `station/shore_station`、`utc/utc_seconds`、
`lat/latitude`、`lon/lng/longitude`），分隔符自动识别 `, ; \t |`：

```csv
station,mmsi,utc,lat,lon
SZ01,413000001,1767225600,22.500000,114.200000
```

* `utc` 为 UTC 秒（Unix epoch，2000–2100 年之间）；
* `mmsi` 为纯数字；
* 纬/经度有限且在合法范围内，且不是 (0,0)；
* 坏行不会中断整批，而是逐行记录在批次页的“解析拒绝行”中。

## 判定规则（`app/ais.py`）

按 MMSI 全量重算，与批次到达顺序无关：

1. **同秒同位置（重投）**：同一船同一 UTC 秒内所有候选位置一致时，只保留一个
   可信候选（先到者胜），其余标记 `duplicate` 隔离。重投不会制造伪轨迹。
2. **同秒异位（冲突）**：该秒只要出现两个不同位置，**该秒所有记录**都标记
   `conflict` 隔离。
3. **速度门限**：候选按 UTC 排序，与**上一可信点**计算的航速超过 **60 节**即
   标记 `speed_exceeded` 隔离；被隔离的点不参与后续“上一可信点”的计算。
4. **迟到电文**：后到的早期记录会触发该船**全部已发布行**的重算，旧点的
   可信/隔离结论及后续速度基准都可能改变；每次重算生成新的船舶 `version`。
5. 位置按 6 位小数（约 0.11 m）比较，恰好覆盖同一次定位的重投抖动。

## 发布与一致性（`app/store.py` / `app/db.py`）

* 原档先写入 `originals/.staging-*/` 并 `fsync`，再原子 `rename` 到
  `originals/<batch_id>/original.csv`，随后才在**单个 `BEGIN IMMEDIATE` 事务**
  中写入批次、原始行、拒绝行、每船判定与可信点并提交。
* 因此“已提交”即“原档已落盘且判定完整”；失败上传整体回滚并删除该目录，
  既有已发布航迹完全不变。
* 查询与下载在 `BEGIN`（WAL 快照）内固定船舶 `version`，并发重算不会让一次
  请求混入新旧两版数据；旧版本在事务内被裁剪。
* 启动时 `cleanup_staging` 清理崩溃遗留的 `.staging-*` 和无批次引用的目录，
  已发布批次永不删除。

## 每日网格密度发布（`app/grid.py` / `app/store.py`）

给港口规划组交付的是**活动密度**而不是单船航迹，口径与隐私规则如下：

1. **固定网格**：经纬度各按 0.1° 划分（用整数微度计算，边界精确，负坐标向下取整），
   格点归属与浮点误差无关。
2. **按 UTC 日、按不同 MMSI 计数**：只取当天可信点（`track_points`），同一 MMSI
   当天在同一格内出现多次只计一次；跨 UTC 日的点不计入。
3. **k=3 隐藏**：不同 MMSI 不足 3 船的格子**在聚合阶段即丢弃**——不写页面、不写 CSV、
   也不写数据库（`grid_cells` 带 `CHECK (distinct_vessels >= 3)` 且无 MMSI 列），
   因此下载接口和页面都无法泄露被隐藏格子的计数或船号。
4. **首次发布固化**：每个 UTC 日只有一行 `grid_publications`，CSV 落盘在
   `data/grids/grid_<day>.csv`。重复发布直接返回既有固化档（响应标记为复用），
   后到电文触发航迹重算也**不会**改动任何已固化的网格档。
5. **同一已提交状态取数**：聚合与登记在同一个 `BEGIN IMMEDIATE` 事务内完成，
   与上传发布互斥，读不到半个批次；事务内记录 `source_snapshot`（批次数、最大原始行
   ID、可信点数、船舶数）作为取数快照指纹。
6. **两实例同日发布只有一份结果**：`grid_publications.day` 为主键，两个服务实例
   （共享同一 SQLite 数据卷）并发发布同一天时，一个插入成功，另一个复用已固化行，
   最终只有一份完整 CSV。
7. **失败不留残档、可重试**：CSV 先写 `.staging-*.tmp` 并 `fsync`，再 `rename` 到
   目标名；写盘或登记任一步失败都会回滚事务并删除文件，已固化旧档与既有航迹不变，
   重试从原状态重新开始。启动时清理 `.staging-*`、`.tmp` 及无登记引用的网格文件。

CSV 格式（无 MMSI 列）：

```csv
utc_date,cell_lat_min,cell_lat_max,cell_lon_min,cell_lon_max,distinct_vessels
2026-01-02,22.5,22.6,114.2,114.3,4
```

当天没有任何格子达到 3 船门槛时，CSV 只含表头，页面明确提示无格子数据，
同样不暴露任何隐藏计数。

### 双实例部署演示

默认 `docker compose up --build` 启动单实例。需要验证“两个服务实例同时发布同一天
只能得到一份完整结果”时，用 `ha` profile 启动共享同一数据卷的第二个实例：

```bash
docker compose --profile ha up --build
# 实例 1: http://127.0.0.1:8000   实例 2: http://127.0.0.1:8001
# 两边同时 POST /grids/publish 同一天，最终 /data/grids/ 下只有一份 grid_<day>.csv
```

## 页面与下载

| 路径 | 作用 |
| --- | --- |
| `GET /` | 批次列表与船舶列表 |
| `POST /upload` | 上传原档（multipart 字段名 `file`） |
| `GET /batches/{id}` | 本批原始行逐行判定 + 解析拒绝明细 |
| `GET /batches/{id}/original.csv` | 下载该批原档（响应头 `X-Content-SHA256`） |
| `GET /vessels/{mmsi}` | 可信点（含相对上一点航速）与隔离原因 |
| `GET /vessels/{mmsi}/track.csv` | 可信航迹 CSV（按 UTC 排序） |
| `GET /vessels/{mmsi}/isolated.csv` | 隔离记录与原因 CSV |
| `GET /grids` | 已固化的每日网格发布列表 |
| `GET /grids/publish` | 选择 UTC 日的发布表单 |
| `POST /grids/publish` | 发布某日网格（表单字段 `day=YYYY-MM-DD`；已发布则复用旧档） |
| `GET /grids/{day}` | 当日可见格子与不同船舶数（隐藏格与船号不出现） |
| `GET /grids/{day}/grid.csv` | 下载当日固化 CSV（响应头 `X-Content-SHA256`） |
| `GET /healthz` | 健康检查 |

## 测试与样例

```bash
. .venv/bin/activate
pytest -q            # 规则 + HTTP/并发/重启/失败回滚 + 网格隐私/固化/双实例
python tests/smoke.py  # 用 samples/ 跑一遍并打印每船判定
```

`samples/README.md` 详细说明了三个样例场景。手工验证顺序：

1. 上传 `samples/batch1_base.csv`：413000001 出现 `duplicate`，413000002 整秒
   `conflict`，413000003 出现 `speed_exceeded`，413000004 的坏行进入拒绝明细。
2. 上传 `samples/batch2_late_arrivals.csv`：413000005 补入迟到点后被重算，
   413000001/413000002 的重投结果保持稳定，同时出现新船 413000008。
3. 对比每次上传后 `/vessels/{mmsi}` 与 `track.csv`：413000006 在批次 2 后其
   01:00 的旧点变为隔离；同时旧批次原档仍可完整下载。

## 目录

```
app/
  ais.py        CSV 解析、校验、纯函数航迹判定
  grid.py       0.1° 固定网格、UTC 日口径、k=3 隐藏与 CSV 渲染（纯函数）
  db.py         SQLite schema、WAL 连接、读写事务
  store.py      原档/网格原子落盘 + 单事务发布 + 全船重算
  main.py       FastAPI 路由与 CSV 导出
  templates/    网页（总览/批次/船舶/网格/错误）
samples/        重投、冲突、迟到点、坏行样例
tests/          规则单测、HTTP/并发/重启/回滚、网格隐私与固化测试、smoke
```
