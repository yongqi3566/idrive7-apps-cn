# 简体中文预编译源

本 fork 的 `Publish Chinese Store source` workflow 构建不含 BMW SDK 的简体中文应用包，
连同预编译 renderer、inputgate 工具包发布到 GitHub Releases。车机在安装时从原厂应用
生成自己的 SDK 模块，不执行应用的 Python 构建。`mgu-repo.json` 继续用于源码渠道。

## 首次使用

1. 在 fork 的 GitHub **Actions** 页面启用 workflows。
2. 选择 **Publish Chinese Store source → Run workflow → main**。后续 `main` 上
   `apps/`、`kit/`、`mgu-repo.json` 或发布 workflow 的变更会自动触发构建和发布。
3. 等待 `build`、`publish` 两个 job 成功，确认 Releases 中出现 `catalog` 和
   `cn-build-<运行序号>-<尝试序号>`。
4. 在车机 Store 的 **Add source** 中添加 `yongqi3566/idrive7-apps-cn`，选择 **Releases**。

选择分支或 Tags 仍然会使用源码构建。Actions 的运行附件只用于传递构建结果，车机实际下载
GitHub Release 附件。工作流使用仓库自带的 `GITHUB_TOKEN`，发布 job 需要 `contents: write`；
如果组织策略禁止写入，需在仓库设置中允许。私有仓库在车机端需要能访问它的 GitHub token。

## 版本和依赖

- 每个应用使用自己的 `apps/<id>/app.json` 中的 `version`，格式须为 `x.y.z`。包内
  manifest、RHMI 元数据和 catalog 的版本一致，不添加语言后缀，也不使用 Actions 序号。
- Release 的 `cn-build-<运行序号>-<尝试序号>` 只标识发布批次，与应用版本无关。
  Sync 或重跑 workflow 不会自动提高应用版本；Store 只在来源版本高于已安装版本时提示更新。
- 每次发布都使用当前源码构建应用包、图标和元数据。即使应用版本没变，也发布本次
  构建的内容，不复用旧包、不自动递增版本。Store 按版本号判断更新，同版本内容变化
  不会提示升级，需要重新安装才能获取新内容。
- renderer、inputgate 的版本严格来自各自 `component.json`，保留安装钩子。应用的 `requires`
  保留，catalog 同时列出两个工具；renderer 也声明 `renderer` 能力。
- 发布前检查 renderer 的源码指纹与预编译二进制是否匹配。不匹配或缺少二进制时，
  Actions 使用上游 Ubuntu 18.04 Docker 环境编译 x86-64 renderer、inputgate，执行
  `check-target` 和 renderer 测试后更新本次打包文件；失败时停止发布。产物不提交回 Git。
  renderer 的配套脚本也从当前源码刷新；指纹检查继续保留。
- 工具代码变化但上游版本没变时，仍按该版本编译发布，包内 manifest、文件名和 catalog
  与上游版本保持一致。此前自动递增的工具版本根据旧 catalog 的 `sourceVersion` 一次性
  恢复为上游版本；例如 renderer 从 `2.1.20` 恢复为 `2.1.19`。已安装较高版本的用户
  需要重新安装以恢复版本一致。继续拒绝真正的源版本倒退。
- 应用 ID 与上游相同。本源不是已安装的上游源的自动替代品；切换来源前需在 Store
  中移除旧来源的同 ID 应用，再从本源安装。移除 source 本身不会卸载应用。需要两份
  应用并存时，应另行分配独立 ID。

旧渠道曾统一使用 `1.0.N`。首次切换到 `app-json` 版本策略时允许一次应用版本迁移，
之后继续严格禁止版本倒退。目前 `live_adjustments` 的源版本为 `0.9.6`，低于旧的
`1.0.2`；如果已经安装了旧统一版本，需在 Store 移除并重新安装该应用一次。

## 发布顺序

构建和检查成功后，先把应用包、图标、工具包上传到本次独立的 draft Release；生成指向
实际附件的 catalog 前，先公开该 Release 并重新获取附件地址，避免保存草稿的
`untagged-...` 临时地址。逐个下载并校验包和图标的大小、SHA256 后，最后更新
`catalog` Release 的 `catalog.json`。
新包准备好前车机一直读取上一份 catalog。公开仓库使用浏览器下载地址，私有仓库使用
需要 token 的 Release asset API 地址。

上传前及更新 catalog 前校验源版本：除上述一次性迁移外，拒绝源版本倒退，允许同版本
内容变化。每批 Release 使用独立 Tag 保存本次包，历史 Release 中的包不会被覆盖。
发布前及切换固定 catalog 前检查源码提交仍是 main 最新提交，防止旧任务覆盖新源码的发布。
本次 catalog 的所有资源都在本次 Release；清理旧 Release 前仍应检查当前
`catalog` 的资源 URL，保留它引用的 Release 以及固定入口 `catalog`。

## 本地验证

需要 Python 3 和 Node.js，不需要提取 SDK；renderer 与源码不匹配时还需要运行中的 Docker。
以下操作只在开发电脑上运行：

```sh
python kit/tools/test_publish_store.py
python kit/tools/test_prepare_store_renderer.py
python kit/tools/test_fit_to_unit.py
python kit/tools/test_id8_ui.py
python kit/tools/test_zh.py
python kit/tools/prepare_store_renderer.py
python kit/tools/publish_store.py --release-tag cn-build-local-1 --repository yongqi3566/idrive7-apps-cn
```

输出在 `dist/store-release/`，输出目录须为空；开发者可选择其他空目录 `--out <目录>`。
本地生成的下载地址是发布前占位，workflow 会按实际 Release API 响应绑定地址。
构建会下载 Doom 在 `app.json` 声明并校验 SHA256 的资源。

云端没有目标车机的 SOME/IP 接口目录，无法完成 `fit_to_unit()` 对具体固件的全部裁剪。
首次使用及上游接口声明变更后，需在目标车机验证安装、启动、图形和控制器输入。
CI 验证包结构、本地化资源及元数据，不能代替实车兼容性验证。

## 跟随上游

发布相关逻辑集中在新增的 workflow、`publish_store.py`、测试和本文档。现有 `build.py`
只扩展 `--without-sdk` 选项，原有构建默认行为不变。同步上游后先检查 Actions；即使
没有 Git 文本冲突，上游改变 manifest 或包结构也可能需要调整发布脚本。

## GitCode 镜像

`Sync GitCode mirror` 是独立的单向同步流程，不在 GitCode 重新编译。GitHub 仍是主仓库：
任意分支推送或 Tag 推送后同步 Git 分支和标签；`Publish Chinese Store source` 成功后，
同步固定 `catalog` 以及它引用的当前版本 Release、说明和附件，不在云端补传无关历史版本。
也可以手动运行同步 workflow 重试当前版本。首次历史补全从本机下载后上传，脚本的
`--all-releases` 选项用于显式补齐所有历史 Release。
普通 Git 推送不触发 Release 同步；发布 workflow 成功后的 `workflow_run` 才触发，
因此不依赖 `GITHUB_TOKEN` 创建 Release 是否产生其他 Actions 事件。

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 中配置：

| 类型 | 名称 | 值 |
| --- | --- | --- |
| Repository secret | `GITCODE_TOKEN` | 有目标仓库 Git 写入和 Release API 权限的个人访问令牌 |
| Repository variable | `GITCODE_REPOSITORY` | `yongqi3566/idrive7-apps-cn` |
| Repository variable，可选 | `GITCODE_USERNAME` | Git HTTPS 认证用户名；默认使用目标仓库所属空间名称 |
| Repository variable，可选 | `GITCODE_HOST` | 默认 `gitcode.com`，也支持 `atomgit.com` |

先创建公开的目标仓库，推荐空仓库；目标分支和标签必须与 GitHub 一致或可快进。
脚本不强推业务分支、不删除目标独有分支和 Tag，不覆盖内容不同的已发布应用包。
目标保护规则如果阻止正常推送，需要在 GitCode 调整机器人账号权限。

同步下载 GitHub 最终发布的附件，确保镜像与 GitHub 本次发布的包完全一致。
先核对源 catalog 的大小和 SHA256，再逐个上传并匿名下载校验 GitCode 附件。
GitCode catalog 只替换包及图标的 URL，应用版本、文件内容、大小和 SHA256 不变。
下载地址使用官方 Release 附件下载 API，避免把会过期的对象存储上传签名保存到 catalog。

每个 GitCode Release 都包含对应的 catalog 副本，固定 `catalog` Release 最后更新。
由于替换 Release 附件需要删除旧附件再上传，车机应使用独立生成分支 `store-catalog`
中的 `catalog.json` 作为 HTTPS 源入口。该分支只在所有包和 Release catalog 校验成功后
提交更新；GitHub catalog 中途变化、应用版本倒退、包下载失败时保留上一版分支入口。
分支更新使用带旧 SHA 的 lease，拒绝并发旧任务覆盖；重复同步相同内容不新增入口提交。

GitCode 成功同步后，在 Actions 摘要查看经过匿名下载校验的 **Store HTTPS source** 地址，
在 Store 添加 HTTPS 源。默认地址为：

```text
https://api.gitcode.com/api/v5/repos/yongqi3566/idrive7-apps-cn/raw/catalog.json?ref=store-catalog
```

不要把 GitCode `owner/repo` 填入 Store 的 GitHub Releases 渠道。首次联调必须确认目标平台
的 API、Git 认证、附件下载和上述 raw 地址可匿名使用；工作流只在最终入口下载校验成功后
报告同步成功。`store-catalog` 由同步脚本维护，不要在 GitHub 创建同名分支或手动编辑它。

本地测试（不连接 GitCode、不需要 Token）：

```sh
python kit/tools/test_sync_gitcode.py
```
