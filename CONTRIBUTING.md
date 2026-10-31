# 贡献指南（Contributing）

> **本仓库只接受 Pull Request。** Issues 与 Discussions 均已关闭：
> 缺陷、需求、方案讨论一律以 PR 的形式提出——PR 描述里写清问题与验证方式，代码本身就是提案。

## 合入规则

- `main` 受保护：**任何改动都必须走 PR**，包括维护者自己（禁止直接 push / 强推 / 删除分支）。
- 一个 PR 只做一件事。夹带无关重构、格式化风暴或"顺手改个名字"的 PR 会被要求拆开。
- CI 必须全绿（`ci / test` 覆盖 CPython 3.9–3.14，`ci / smoke` 验证进程能起来）；
  `policy / pr-metadata` 校验 PR 模板的必填项。
- 合并方式为 squash merge，commit message 使用英文 [Conventional Commits](https://www.conventionalcommits.org/)：
  `fix(server): drain request body on 401`。

## 开发流程

```bash
git clone https://github.com/minc-nice-100/step2api.git
cd step2api
python -m unittest discover -s tests -v      # 全量测试，无需网络、无需凭证
python -m pyflakes step2api step2api.py tests scripts
```

- 运行时**只允许标准库**（`http.server` / `urllib` / `ssl` / `json` …），Python 3.9+。
  新增第三方依赖的 PR 不会被接受；`scripts/sdk_check.py` 里的可选依赖只用于自检脚本。
- 测试必须离线可跑：`tests/test_e2e.py` 自带 fake 上游，真实上游只在 `--doctor` / `smoke.py` 里访问。
- 上游协议细节（鉴权、SSE 事件、错误码）见 [CLAUDE.md](CLAUDE.md)；改动协议层请同步更新那里的不变量。
- 不要在仓库里提交凭证：`accounts/`、`usage.jsonl`、`.probe/`、`.testdata/`、`*.log` 都已被忽略，
  提交前用 `git diff --cached` 自查，泄漏过的 Cookie 请立即在浏览器侧重新登录使其失效。

## AI 生成代码政策（强制）

允许使用 AI 助手写代码，但**必须随附单元测试情况**，否则 PR 直接关闭：

1. 在 PR 模板的「AI assistance disclosure」里勾选对应项，写明 AI 生成的**范围**（文件/函数）；
2. 写明覆盖这些代码的**测试**（`tests/xxx.py::ClassName.test_name`）；
3. 粘贴**实际跑过的测试命令与结果**（通过数量 / 失败数量 / Python 版本 / commit）。
   只写"测试应该没问题"、只贴截图、或把未运行的测试写成已通过，都视为未提供；
4. 纯文档 / 注释类改动可以不附测试，但要在 PR 里说明"无行为变更"，并且仍要声明是否由 AI 生成。

评审时会做"对质"：改动代码语义，看对应单测是否转红。测试全绿但改坏了逻辑却测不出来，
说明测试本身不合格，PR 会被打回补测试。

## 报告安全问题

不要开 PR 来暴露漏洞。使用 GitHub 的私密漏洞报告：
<https://github.com/minc-nice-100/step2api/security/advisories/new>

## 许可

提交即表示你同意以 [Unlicense](LICENSE)（公有领域）发布你的贡献。
