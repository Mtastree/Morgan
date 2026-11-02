# 开发与测试

源码产品入口为 `hermes_lite.chatdesktop` 和 `hermes_lite.chatweb`。此源码整理版没有完整 Hermes Agent、工具执行、自进化实验或 vendor_core；当前聊天入口不依赖它们。

## 源码检查

```powershell
python -B scripts/check_source_bundle.py
python -B scripts/init_local_config.py --dry-run
```

检查器核验语法、相对导入闭包、禁止载荷和常见凭据格式，不导入应用、不读取聊天数据库。它不是通用密钥扫描器。

## 隔离模拟测试

先准备 Python 环境。下面命令只使用本机模拟模型和模拟搜索，不要求真实模型服务。测试审计目录每次必须使用新名称，不要覆盖旧证据。

```powershell
cd src\hermes-lite
$env:HERMES_AUDIT_ROUND = 'local_check_001'
& .\.venv\Scripts\python.exe -B scripts\desktop_round60_search_closeout_tests.py
& .\.venv\Scripts\python.exe -B scripts\desktop_round58_search_gap_fill_tests.py
& .\.venv\Scripts\python.exe -B scripts\desktop_round55_search_automation_tests.py
node scripts\desktop_round60_frontend_real_js_tests.js
```

Node 仅用于前端逻辑测试，正常使用应用不需要。测试通过现有数据库保护器拒绝使用生产聊天库。不要擅自删除保护器或改成默认生产路径。

Node VM 使用模拟 DOM/EventSource，直接执行生产 chat.js；这不是浏览器渲染或视觉验收。工程模拟测试也不证明真实模型的事实正确率。
