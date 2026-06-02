FROM python:3.9.5-slim-buster
LABEL author="Lan"
LABEL email="xzu@live.com"

# 设置工作目录
WORKDIR /app

# 设置时区为亚洲/上海，确保容器日志时间与本地同步
RUN ln -sf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime && echo 'Asia/Shanghai' >/etc/timezone

# 【构建加速关键】：先单独复制 requirements.txt 以最大化利用 Docker 镜像缓存
COPY requirements.txt /app/

# 安装依赖。默认使用国内清华源加速，可在构建时通过 --build-arg PIP_INDEX_URL= 覆盖
ARG PIP_INDEX_URL="https://pypi.tuna.tsinghua.edu.cn/simple"
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# 将当前开发的代码拷入容器内
COPY . /app

# 剔除无需在生产环境运行的体积负载，确保镜像极度轻量化
RUN rm -rf docs fcb-fronted readme* LICENSE SECURITY.md

# 暴露接口服务端口
EXPOSE 12345

# 启动服务端应用
CMD ["python", "main.py"]