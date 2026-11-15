# Security Policy

## Supported versions

| Version | Supported |
|---|---|
| 0.53.x | Yes |
| < 0.53 | No |

## Reporting a vulnerability

请不要在公开 issue 里贴漏洞细节、利用代码或敏感数据。

优先使用 GitHub 的私密安全报告渠道：

https://github.com/SiyanHaven/AGL/security/advisories/new

如果该入口不可用，请通过仓库维护者公开资料中的私下联系方式报告。

报告请包含：

- 受影响版本；
- 最小复现步骤或示例 `.agl` / 命令；
- 预期结果与实际结果；
- 影响范围，例如任意文件读写、越权外部调用、VM 逃逸、拒绝服务。

## 范围

重点包括：

- AGL VM 的效应权限、工具白名单与资源限制；
- 编译器对恶意或畸形源码的处理；
- MCP 服务器的输入校验；
- 发布包和安装脚本的供应链问题。

普通语言缺陷、诊断文案错误和不影响安全的兼容性问题，按普通 issue 处理。
