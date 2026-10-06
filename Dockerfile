FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/credits.db

WORKDIR /app

# 依赖安装（利用层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 应用代码与数据库初始化入口
COPY app ./app

# 数据库文件目录：Compose 在此挂载命名卷，重启后记录仍在
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 单 worker：SQLite 写事务在进程内串行化；多 worker 下 BEGIN IMMEDIATE
# 与唯一部分索引同样保证正确性。应用启动时自动建表初始化数据库。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
