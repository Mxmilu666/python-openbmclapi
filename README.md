<div align="center">

![](https://s21.ax1x.com/2024/03/09/pFyV90g.png)

# OpenBMCLAPI for Python

![GitHub Issues or Pull Requests](https://img.shields.io/github/issues-pr/TTB-Network/python-openbmclapi)
![GitHub Issues or Pull Requests](https://img.shields.io/github/issues/TTB-Network/python-openbmclapi)
![GitHub License](https://img.shields.io/github/license/TTB-Network/python-openbmclapi)
![GitHub Release](https://img.shields.io/github/v/release/TTB-Network/python-openbmclapi)
![GitHub Tag](https://img.shields.io/github/v/tag/TTB-Network/python-openbmclapi)
![GitHub Repo stars](https://img.shields.io/github/stars/TTB-Network/python-openbmclapi)
![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/TTB-Network/python-openbmclapi/build_and_publish.yml?label=create%20tagged%20release)
![GitHub Actions Workflow Status](https://img.shields.io/github/actions/workflow/status/TTB-Network/python-openbmclapi/github-code-scanning%2Fcodeql?label=codeql)




✨ 基于 [OpenBMCLAPI](https://github.com/bangbang93/openbmclapi) 的 Python 实现。

🎨 **跨系统**、**跨架构**和 **Docker** 支持。

🎉 __*新增功能！*__ 基于 Echart 的 OpenBMCLAPI 仪表盘（Dashboard）。

🎉 __*新增功能！*__ 基于 loguru 的日志器。

</div>

# 简介

本项目是 [OpenBMCLAPI](https://github.com/bangbang93/openbmclapi) 的 Python 版本，OpenBMCLAPI 是通过分布式集群帮助 [BMCLAPI](https://bmclapidoc.bangbang93.com/) 进行文件分发、加速中国大陆 Minecraft 下载的公益项目。

如果你想加入 OpenBMCLAPI，可以寻找 [bangbang93](https://github.com/bangbang93) 获取 `CLUSTER_ID` 和 `CLUSTER_SECRET`。

# 部署

## 从源码运行

1. 克隆仓库或从 [Releases](https://github.com/TTB-Network/python-openbmclapi/releases) 中下载代码：

    ```sh
    git clone https://github.com/tianxiu2b2t/python-openbmclapi.git
    cd python-openbmclapi
    ```

2. 安装依赖：

    ```sh
    pip install -r --no-deps requirements.txt
    ```

    > 你可能需要先安装 [Microsoft C++ 生成工具](https://visualstudio.microsoft.com/visual-cpp-build-tools/)。

3. 运行一次主程序生成配置文件：

    ```sh
    python ./container/main.py
    ```

4. 在 `config/config.yaml` 中，填写你的 `cluster_id`（即 `CLUSTER_ID`）和 `cluster_secret`（即 `CLUSTER_SECRET`）。

5. 重新启动程序。

## 使用 Docker 部署

1. 拉取镜像：

    ```sh
    docker pull silianz/python-openbmclapi:latest
    ```

    你也可使用镜像源进行拉取：

   ```sh
   docker pull registry.cn-hangzhou.aliyuncs.com/silianz/python-openbmclapi:latest
   ```

2. 创建容器：

    ```sh
    docker run -d \
    -v ${/data/python-openbmclapi}:/bmclapi \
    -e cluster.id=${cluster.id} \
    -e cluster.secret=${cluster.secret} \
    -e web.public_port=${web.public_port} \
    -p ${web.public_port}:80 \
    --restart always \
    --name python-openbmclapi \
    silianz/python-openbmclapi 
    ```

    **参数说明：**

    `web.public_port` - 对外开放的端口。

    `cluster.id` - 即 `CLUSTER_ID`。

    `cluster.secret` - 即 `CLUSTER_SECRET`。

    `/data/python-openbmclapi` - `bmclapi` 文件夹（即缓存 `cache` 文件夹）挂载的路径。

## 配置文件

```yml
# 是否不使用 BMCLAPI 分发的证书, 同 CLUSTER_BYOC
byoc: false
# OpenBMCLAPI 的 CLUSTER_ID
cluster_id: ''
# OpenBMCLAPI 的 CLUSTER_SECRET
cluster_secret: ''
# 同步文件时最多打开的连接数量
download_threads: 64
# 超时时间
timeout: 30
# 实际开放的公网主机名, 同 CLUSTER_IP
web_host: ''
# 要监听的本地端口, 同 CLUSTER_PORT
web_port: 8800
# 实际开放的公网端口, 同 CLUSTER_PUBLIC_PORT
web_publicport: 8800
io_buffer: 16777216
max_download: 64
min_rate: 500
min_rate_timestamp: 1000
port: 8800
public_host: ''
public_port: null
server_name: TTB-Network
```

# 贡献

如果你有能力，你可以向我们的[团队](mailto://administrator@ttb-network.top)或[团队所有者](mailto://silian_zheng@outlook.com)发送邮件并申请加入开发者行列。

# 鸣谢

[LiterMC/go-openbmclapi](https://github.com/LiterMC/go-openbmclapi)

[bangbang93/openbmclapi](https://github.com/bangbang93/openbmclapi)

[SALTWOOD/CSharp-OpenBMCLAPI](https://github.com/SALTWOOD/CSharp-OpenBMCLAPI)
