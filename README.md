# Drive9 + AWS Bedrock AgentCore Runtime Instances（ARM64）

本 Demo 在 AWS `us-east-2` 部署三个 AgentCore Runtime，验证 Drive9 CLI/API 在 Runtime 中的文件操作、持久化、跨 Agent 交接、权限和检索能力。Docker 镜像与 Capacity Provider 固定使用 ARM64；本项目不执行 CPU benchmark 或 CPU 配额测试。

## 入站授权模型

Runtime 使用 AgentCore `customJWTAuthorizer` 校验 OIDC JWT，并在应用内再次校验签名、issuer、有效期、audience/client 和 scope。单独拥有 IAM `bedrock-agentcore:InvokeAgentRuntime` 不再能访问这些 JWT Runtime。18 个 action 按最小权限分为四级：

| Scope | 可用能力 |
|---|---|
| `drive9.read` | read、list、stat、find、search |
| `drive9.write` | read 级能力，以及 write、append、copy、move、handoff |
| `drive9.operator` | write 级能力，以及 preflight、delete、permission/visibility test |
| `drive9.admin` | 全部能力；额外允许 root mount 和 Git 验证 |

未知 action 不再返回 action 清单；授权拒绝发生在 handler 执行前。permission/visibility 的预期结果由 Runtime 配置决定，不再信任请求 payload 中的 `expect_*` 字段。

所有调用方提供的 Drive9 路径必须是相对于当前 Runtime `BASE_REMOTE` 的相对路径。`:/...`、POSIX 绝对路径和包含 `..` 的路径都会在执行 Drive9 命令前被拒绝；因此 action 不能跳出 agent-a/b 的共享前缀或 agent-c 的隔离前缀。

## 已验证结论

- EC2、AWS CLI 和 AgentCore Runtime Instances API 位于 `us-east-2`。
- ARM64 Capacity Provider 使用 `LINUX_ARM64`，默认实例类型为 `c6g.large`。
- Drive9 CLI 使用 `DRIVE9_API_KEY` bearer token 已成功完成文件读写；Runtime 不应把该 token 传给 `drive9 ctx import`。
- 本次测试使用 Anonymous workspace `https://api.drive9.ai`。不要把 `api.drive9.ai` 的 token 与区域 TiDBCloud server 混用。
- 旧 Runtime 曾因镜像中的 `SyntaxError: '(' was never closed` 启动失败；必须重新构建并推送 ARM64 镜像后再验收。
- agent-a 和 agent-b 必须使用同一个可读写 scoped token，并只访问共享目录；agent-c 必须使用独立的只读 scoped token 和独立目录，同时验证其无法列出 A/B 共享目录。

## 最近一次 AWS 验收

2026-09-07 在 `us-east-2` 使用全新 ARM64 镜像和全新 Runtime 完成可见性隔离验收。agent-a 与 agent-b 使用同一个可读写 `DRIVE9_API_KEY`，并看到相同的 `shared/functional/` 目录内容；agent-c 使用独立只读 key，只授权自己的目录，访问 A/B 的 `shared` 目录得到 `not found`，无法发现该目录。新镜像 digest 为 `sha256:371a9c3da46ef810ba2417bfd78885bd81c762e60495a7bf51c7e90941840286`。回归结果为 27 项 `PASS`、3 项 `PLATFORM_BLOCKED`、0 项 `FAIL`，整体结论为 **CONDITIONAL GO**。完整报告和打印日志见 `artifacts/agentcore-20260907T133955Z-visibility/`。

本次验收修正了三处环境兼容问题：Runtime 执行角色增加 `bedrock-agentcore.amazonaws.com` trust principal；Secret 默认名改为包含 `TEST_RUN_ID`，避免误用旧 token；调用脚本兼容 AWS CLI `invoke-agent-runtime` 的 positional 输出文件和 boto3 1.42.x 环境。

如果 Runtime 报 `Drive9 secret JSON must contain token or api_key`，先检查 Secret 中 token 是否为空。`create-secret` 成功不代表 token 有效；必须在生成 token 后检查文件非空，并在写入 Secret 后检查 `.token | length > 0`。

## 文件

- `app.py`：Runtime handler；从 Secrets Manager 读取 Drive9 token，执行 CLI action。
- `deploy.py`：创建 ARM64 Capacity Provider 和三个 Runtime，支持 SDK/CLI control plane。
- `runtime_oauth_client.py`：使用短期 Bearer Token 调用 JWT 授权的 Runtime。
- `tests/invoke_action.py`：单 action 调用，默认使用 `--method oauth`。
- `tests/run_tests.py`：OAuth 回归测试和 Markdown/JSONL 证据输出。
- `scripts/create_capacity_provider_operator_role.sh`：使用 AWS CLI 创建 Capacity Provider Operator Role。
- `docs/EXECUTE_TESTS.md`：完整执行、验收、清理和故障排查步骤。
- `demo/reinvent_demo.py`：re:Invent 舞台演示脚本，覆盖跨 Runtime 持久化、交接、检索、权限隔离与新 Session 恢复。
- `scripts/record_reinvent_demo.sh`：运行演示并生成 macOS 屏幕录像和终端 transcript。

## 快速开始

```bash
export AWS_REGION=us-east-2
export DRIVE9_SERVER=https://api.drive9.ai
export AUTHZ_DISCOVERY_URL=https://<issuer>/.well-known/openid-configuration
export AUTHZ_ALLOWED_CLIENTS=<oauth-client-id>
python3 -m py_compile app.py deploy.py runtime_oauth_client.py tests/invoke_action.py tests/run_tests.py
```

随后按 [`docs/EXECUTE_TESTS.md`](docs/EXECUTE_TESTS.md) 构建 `linux/arm64` 镜像、推送 ECR、创建 Secret、部署 Runtime，并执行冒烟和回归测试。

re:Invent 演示的彩排、现场执行和录制步骤见 [`demo/README.md`](demo/README.md)。
