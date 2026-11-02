# 安装与运行

## 准备

当前记录的验证环境是 Windows 和 Python 3.14。需要 Microsoft Edge WebView2 Runtime，以及与你的硬件兼容的 llama.cpp Windows 发行文件。其他 Python 版本及全新机器的安装组合尚未实测。

`setup.cmd` 默认调用 `py -3.14` 创建 `src/hermes-lite/.venv`，随后安装桌面依赖。已选定解释器时可运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -Python C:\Python314\python.exe
```

脚本不下载模型和 llama.cpp，不启动应用，也不覆盖已有配置。`-WithSearch` 安装可选搜索依赖；安装依赖需要网络或提前准备的包源，日常本地推理与安装阶段是两个阶段。

## 配置

首次初始化会将 `configs/*.example.*` 中的部署、模型来源、采样与空人格配置复制为本地文件。实际文件被 `.gitignore` 排除。

`model_profiles.example.json` 仅作多模型结构参考，不自动拷贝。首次启动会根据 deployment 初始化注册表，之后可在设置中添加其他 GGUF 模型，避免复制旧机器的绝对路径和运行状态。

示例模型文件为 `models/qwen2.5-1.5b-instruct-q4_k_m.gguf`，推理程序为 `runtime/llama.cpp/llama-server.exe`。示例来源元数据保存了原部署使用的固定版本与哈希，仅用于对应文件，不适用于自行替换的模型。

## 入口

| 入口 | 用途 |
|---|---|
| `desktop.cmd` | 桌面主入口，按配置管理模型进程 |
| `start.cmd` / `stop.cmd` / `status.cmd` | 手动控制推理服务 |
| `chatpage.cmd` | 本地网页聊天，需要模型服务可用 |
| `chat.cmd` | 命令行直连聊天 |

网页聊天默认使用本机 8771 端口，模型示例端口为 18085。出现端口冲突时先检查服务归属，不要强制终止未知进程。

## 保存与迁移

这份目录保存源码，不能替代完整的个人数据备份。实际聊天库、记忆、人格和本机配置保存在各自运行目录中，均不纳入源码上传。重新部署时需要重新准备环境、模型和本地配置。
