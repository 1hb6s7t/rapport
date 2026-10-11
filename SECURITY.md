# Security Policy

## 威胁模型 / Threat model

Rapport 是单用户、本机运行的工具。我们防护的是：

- 同一台机器上的其他网页通过浏览器访问本地端口（Host/Origin 白名单、配对会话、CSRF 头、`SameSite=Strict` Cookie）。
- 聊天原文与 API Key 明文落盘（Fernet 加密的 blob 与 secrets 文件，密钥文件 0600）。
- 聊天原文进入日志、导出、诊断包、URL（设计上不允许）。
- 模型输出被当作代码执行或渲染为 HTML（模型无工具，前端一律转义渲染）。
- 上传恶意文件（MIME 与像素上限校验，路径穿越防护）。

我们**不**防护：已被攻陷的主机；操作系统管理员；磁盘/内存取证级的数据恢复；你已经发送给模型供应商的数据；你把服务暴露到公网的情形。

## 报告漏洞 / Reporting

请不要在公开 Issue 中披露安全问题。发送邮件到仓库维护者（见 GitHub 个人资料），或使用 GitHub Security Advisories 私密报告。请附最小化的**合成**复现材料，不要附真实聊天。

我们会在 7 天内确认，并在修复发布前与你协调披露时间。

## 依赖与供应链

- 依赖版本由 `backend/uv.lock` 与 `frontend/package-lock.json` 锁定。
- 发布前运行 `pip-audit` / `npm audit`，结果记录在 CHANGELOG。
- 外部 PR 的 CI 不接触任何 API Key；真实模型测试只由维护者在本机运行。
