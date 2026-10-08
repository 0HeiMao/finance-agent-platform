# 后端镜像：FastAPI + uvicorn，直接跑后端仓库里的代码。
#
# 为什么用 python:3.11-slim 而不是 alpine：`sentence-transformers` 依赖 torch，musl 平台没有预编译轮子，
# 装到最后会退化成源码编译（几十分钟 + 一堆构建依赖）。slim 用的是 manylinux 轮子，装得动。
#
# 镜像里**不含**：模型权重、上传件、测试与前端源码。`data/` 由 compose 以卷挂载进 /app/data：
# data/models/finance-embedding 有 470MB 的 model.safetensors，打进镜像会让每次构建都白搬一遍，
# 而它本来就不在 git 里（.gitignore 忽略 data/models）。
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    # 本地模型必须离线加载：开着 HF 在线检查会在没有外网的机器上卡住启动路径
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

WORKDIR /app

# 先只拷依赖清单：依赖没变时这一层命中缓存，改代码不会重装 torch
COPY requirements.txt ./

# torch 必须先单独装 CPU 版：PyPI 上 linux x86_64 的默认 torch 是 CUDA 版，
# 会顺带拖 nvidia-cudnn / nvidia-cublas 等 ~5GB 轮子，而这个服务只做 CPU 推理
# （本地 SentenceTransformer 算 384 维向量）。先装好 CPU 版后，
# `pip install -r requirements.txt` 看到 torch 已满足就不会再换成 CUDA 版。
# 实测：CUDA 版单是 cudnn 一个包就 553MB，且构建时间多十几分钟。
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch

RUN pip install --no-cache-dir -r requirements.txt

COPY backend ./backend
COPY scripts ./scripts

EXPOSE 8000

# --proxy-headers/--forwarded-allow-ips：容器前面是 nginx，不解析 X-Forwarded-For 的话
# 审计日志与登录限流看到的来源 IP 会全是 nginx 容器地址。
# 代价：能直连 8000 的人可以伪造这个头——所以生产只暴露 frontend，后端不对外发布端口。
CMD ["python", "-m", "uvicorn", "backend.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
