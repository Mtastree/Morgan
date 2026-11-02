# Morgan

面向本地小语言模型的 Windows 桌面聊天应用。通过 llama.cpp 在本机推理，提供聊天记录、可纠正记忆、人格文件、上下文摘要、模型配置切换及可选网页搜索。

这是当前聊天产品的源码整理版。早期 Hermes Agent 实验、模型文件、运行环境、个人数据和历史审计载荷不在本目录中。

## 主要功能

- 桌面窗口与本地网页聊天入口。
- 本地聊天记录、偏好记忆及人格文件编辑。
- 上下文和回复预算设置、长对话摘要。
- 多模型配置管理、受管模型加载与切换。
- 按需网页搜索、资料不足时的一次补查及停止生成。

记忆与人格通过本地数据及提示词生效，不更新模型权重。搜索关闭时按本地聊天流程运行；开启搜索会向外部搜索服务发送查询，不能将该模式称为完全离线。

## 开始使用

当前验证环境为 Windows、Python 3.14、pywebview/WebView2 和 llama.cpp。源码不包含 Python、WebView2、模型或推理程序，需自行准备；尚未在全新电脑上完成安装验证。

```powershell
# 创建环境、安装桌面依赖并初始化配置；这个步骤需要可用的包来源
.\setup.cmd

# 如需网页搜索，可选择安装搜索依赖
.\setup.cmd -WithSearch
```

将模型放入 `models/`，将兼容的 Windows llama.cpp 推理运行文件放入 `runtime/llama.cpp/`，再检查 `configs/deployment.json`。运行时通常需要整个相应发行包的依赖文件，不能只拷贝一个 exe。

```powershell
.\desktop.cmd
```

建议由桌面直接启动并管理模型。若先手动运行 `start.cmd`，桌面可能将其视为外部模型服务，模型切换会受到进程归属保护。

详细说明见 [安装](docs/INSTALL.md)、[开发与测试](docs/DEVELOPMENT.md)、[数据与联网](docs/PRIVACY.md) 和 [整理范围](docs/SOURCE_LAYOUT.md)。

## 模型

提供 Qwen2.5-1.5B-Instruct Q4_K_M 的通用配置示例，以及可选 3B 配置示例。窗口、GPU 卸载和回复上限需要按模型与硬件调整；示例值不是对其他硬件的性能保证。模型需单独下载，并遵守各自来源与许可。

## 源码结构

```text
configs/                    通用配置示例；实际配置不跟踪
docs/                       当前使用、开发和整理说明
scripts/                    初始化、推理服务控制及源码检查
src/hermes-lite/hermes_lite/ 聊天后端、桌面入口和页面资源
src/hermes-lite/scripts/     隔离测试及模拟服务
models/                     模型放置说明，不含权重
runtime/                    推理程序放置说明，不含二进制
```
