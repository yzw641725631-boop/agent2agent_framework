# Agent Hub 私有语义模型服务

使用 BAAI/bge-small-zh-v1.5 的 Xenova ONNX FP32 转换版本。模型、转换提交、Tokenizer 及 SHA-256 固定在 `model-manifest.json`；服务启动核验随镜像保存的可信清单。

- 原模型：[BAAI 官方模型卡](https://huggingface.co/BAAI/bge-small-zh-v1.5)，MIT 许可证。
- 转换：[Xenova 固定提交](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/75c43b069aac4d136ba6bc1122f995fedcfd2781)。
- 512 维，CLS 归一化；长文按 384 Token 窗口、320 步长编码并平均后再归一化，不静默丢弃尾部。每次最多 32 窗口。
- 部署前运行 `python fetch-model.py /opt/agent-hub/semantic/model-fp32` 并构建本目录镜像 `agent-hub-embedding:0.1.0`。运行中不下载、不加载远程代码。
- 仅内部 Docker 网络，禁止公开端口；无数据库、Git 或身份凭证。正文不写访问日志，遥测关闭，无外网。
- 单线程推理，最多 8 个 HTTP 连接；并发占用时返回 503，由 API 明示降级。容器内存限额 384 MB。

这是检索模型，不生成摘要、审核知识或判断冲突。排序分数不是事实证明。
