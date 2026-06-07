# 2026-06-07 Mainline / Deploy Script Handoff

## 背景

2026-06-07 已把原 `feature/memory-diffusion-p0` 扶正为 `main`，保留 `p0` 分支作为同指针兼容线。这个仓库有 star，主入口继续使用 `Yinglianchun/Ombre-Brain`，不要迁移到 `Haven-Ombre` 作为公开入口。

## 当前远端状态

- `origin/main` = `03e84e8051cfd000c1659e73f067fe73a0183b48`
- `origin/feature/memory-diffusion-p0` = `03e84e8051cfd000c1659e73f067fe73a0183b48`
- `shadow/main` = `03e84e8051cfd000c1659e73f067fe73a0183b48`
- `shadow/feature/memory-diffusion-p0` = `03e84e8051cfd000c1659e73f067fe73a0183b48`
- 旧 `origin/main` 备份：`archive/main-before-p0-20260607` = `bc90714b4c692355bcd522678d2adc4779944bfc`
- 旧 `shadow/main` 备份：`archive/main-before-p0-20260607` = `ed3e514b4fd51ead665044527ca0ba7b7741e767`

当前本地主工作区仍在 `feature/memory-diffusion-p0`，但 `origin/main`、`origin/feature/memory-diffusion-p0`、`shadow/main`、`shadow/feature/memory-diffusion-p0` 都指向同一个提交。工作区只剩未跟踪临时目录：`.codex-remote-attachments/`、`output/`、`tmp/`。

## 已完成

- `e649d7e Update README for mainline handoff release`
  - README 顶部已说明当前 `main` 是新版主线。
  - README 已列出 Handoff / Portrait / Darkroom / Just Now Chat Context 等主线能力。
  - 旧主线留档路径写入 README。
- `03e84e8 Handle mainline reset in deploy scripts`
  - `scripts/_ops_common.sh` 新增 `ombre_update_git_checkout`。
  - `scripts/update_deploy.sh` 不再裸 `git pull --ff-only`，改走 `ombre_update_git_checkout`。
  - `scripts/one_click.sh` 的 Python 直跑更新也改走同一函数。
  - `config.example.yaml` 补齐 portrait 新字段：`source_excerpt_chars`、`recent_continuity_days`。
  - `one_click.sh` 生成的 `config.yaml` 已补新版关键配置：Just Now Context、Date Persona Trace、`direct_render_mode`、`retrieval_mode`、portrait memory、Daily Portrait Maintainer。
  - README 更新了旧 `main` 部署目录的更新方式：不要手动普通 `git pull`，直接运行 `scripts/update_deploy.sh`。

## 部署脚本当前行为

`ombre_update_git_checkout` 的目标是避免旧 `main` 用户在主线换轨后手动 merge 出大量冲突。

- 默认远端：`OMBRE_REMOTE`，未设置时为 `origin`。
- 默认目标分支：`OMBRE_BRANCH`，未设置时为当前分支；detached 时为 `main`。
- 能 fast-forward：直接 `git merge --ff-only FETCH_HEAD`。
- 本地 ahead：停下，要求先 push 或归档本地提交。
- 本地和远端分叉：
  - `OMBRE_ALLOW_DIVERGED_RESET=1` 或未设置：如果 tracked 文件干净，创建本地备份分支 `archive/local-<branch>-before-reset-<timestamp>`，然后 `git reset --hard FETCH_HEAD`。
  - tracked 文件有本地改动：停下，不碰。
  - `.env`、`buckets/`、`state/` 这类未跟踪/挂载文件不参与 tracked 改动检查，不会被 reset 删除。

## 已验证

- Git Bash 显式路径语法检查：
  - `C:\Program Files\Git\bin\bash.exe -n scripts/_ops_common.sh scripts/update_deploy.sh scripts/one_click.sh`
- 空白检查：
  - `git diff --check`
- YAML 检查：
  - `config.example.yaml` 可被 PyYAML 解析。
  - `gateway.just_now_context_enabled == true`
  - `portrait.recent_continuity_days == 3`
- 临时 Git 仓库模拟：
  - 旧部署目录停在旧 `main`。
  - 远端 `main` 被替换成另一条历史。
  - 调用 `ombre_update_git_checkout` 后成功创建 `archive/local-main-before-reset-*`，并 reset 到新 `main`。

没有部署 VPS；这轮是 README、模板和部署脚本更新，没有运行时代码改动。

## 仍然不全的地方

下一窗口继续补脚本和 README 时，建议先看这些点：

- README 仍然偏长、历史痕迹多；需要系统性过一遍，把“p0 实验线”的旧语气改成“当前 main 主线”，但历史文档里的 p0 记录不要乱改成事实错误。
- README 的配置说明只补了关键字段，还没完整解释 one-click 新生成配置和 Dashboard 可配置项之间的关系。
- `scripts/update_deploy.sh` 现在默认使用当前分支；这对 live p0 兼容，但对公开用户可能更希望默认 `main`。是否改为默认 `OMBRE_BRANCH=main` 需要再确认。
- 主线换轨 reset 目前是 tracked 文件干净时自动执行；如果担心用户害怕，可以在交互式 one-click 里加提示，在非交互 `update_deploy.sh` 保持自动。
- one-click 生成的 `connection_guide.txt` / 客户端提示还没同步写新窗口 handoff、query breath、darkroom/tool guide 这些新版使用方式。
- `docs/Tool Guide.md` 已经短过一版，但 README 里的工具说明仍可能和最终外部暴露工具不完全一致。尤其暗房计划外部只暴露 `darkroom_enter`。
- 旧 worktree：
  - `D:\Ombre-Brain-main-investigate` 还在旧 `main` 本地状态，可以当旧主线留档；不要在里面误做主线工作。
  - `D:\Ombre-Brain-shadow-sync` 也落后，不要用它直接同步，除非先确认用途。

## 下个窗口建议顺序

1. 先重新 `git fetch --all --prune`，确认 `origin/main`、`origin/feature/memory-diffusion-p0`、`shadow/main`、`shadow/feature/memory-diffusion-p0` 都在同一 HEAD。
2. 检查 `README.md` 全文中的 `p0`、`feature/memory-diffusion-p0`、`main 旧桶召回`、`旧版/新版` 等词，区分历史记录和当前发布说明。
3. 检查 `scripts/one_click.sh` 的生成配置、生成提示、更新菜单文字，不要只改 `config.example.yaml`。
4. 如果继续改部署脚本，优先补可读提示和测试场景，不要把 reset 逻辑扩成复杂迁移器。
5. 改完后至少跑：
   - `C:\Program Files\Git\bin\bash.exe -n scripts/_ops_common.sh scripts/update_deploy.sh scripts/one_click.sh`
   - `git diff --check`
   - PyYAML 解析 `config.example.yaml`
   - 临时 Git 仓库模拟旧 `main` 换轨。
