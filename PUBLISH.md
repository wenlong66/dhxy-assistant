# 创建公开 GitHub 仓库并提交

## 当前状态

此包是发布准备包，**不表示 GitHub 仓库已经创建**。原会话的 GitHub 工具只有读取能力，未执行远端创建或推送。
默认目标是个人账号 `wenlong66` 下的新公开仓库 `dhxy-assistant`。

## 本机运行

需要 Python 3.11+、Git 和 GitHub CLI。首次缺少 Git 或 GitHub CLI 时，可在 Windows 本机安装：

```powershell
winget install --id Git.Git --exact
winget install --id GitHub.cli --exact
```

安装后重新打开终端，进入解压后的 `dhxy-assistant` 目录：

```powershell
py publish_github.py
```

脚本会复跑离线测试，未登录时启动 GitHub 官方浏览器登录流程，然后核对登录账号。
不需要创建 OpenAI 密钥，不会运行付费答题请求，不需要把 GitHub token 发到聊天。

它会建立 `main` 分支和初始提交，创建**公开**仓库，推送代码，再核对远端提交 SHA。
只有核验成功才输出成功信息，并把结果保存在被 Git 忽略的 `publication-result.json`。

仅做本地检查、不连接 GitHub：

```powershell
py publish_github.py --check-only
```

默认仓库名被占用时，脚本拒绝改动现有仓库；可在新的解压目录指定另一个名字：

```powershell
py publish_github.py --repo dhxy-assistant-quiz
```

## 公开内容与保护

只提交脚本中明确列出的源文件、说明、示例题目和测试，不使用 `git add .`。
不上传原始游戏截图、角色账号、余额、运行时数据库、日志或密钥。
内置模式扫描只是辅助检查，不能替代完整安全审计；修改代码后应自行检查公开内容。

提交身份在本仓库局部设置为当前账号与 GitHub no-reply 地址；不修改全局提交姓名或邮箱。
凭证由本机 GitHub CLI 管理，项目内不保存 token。脚本不设置许可证，不建立付费服务。

## 失败恢复

脚本拒绝覆盖已有 Git 仓库或已有同名远端仓库，也不会 force-push、删仓库、回滚提交或无限重试。
如果创建成功但推送失败，远端可能留下空仓库，本地保留初始提交；请先检查状态，不要盲目重复整个脚本。
确认 `origin` 确实指向你刚创建的目标仓库后，可运行：

```powershell
git remote -v
git status
git push --set-upstream origin main
```

如果尚未添加 `origin`，先在 GitHub 确认仓库身份，再按实际 URL 添加；不要覆盖已有 remote。
发布脚本做过本地检查及模拟流程测试，未在真实 Windows / GitHub 写入环境中端到端运行。

## 官方命令参考

- GitHub CLI repository creation: https://cli.github.com/manual/gh_repo_create
- GitHub CLI authentication: https://cli.github.com/manual/gh_auth_login
- Adding local code to GitHub: https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github
