# 简体中文预编译源

本 fork 的 `Publish Chinese Store source` workflow 构建不含 BMW SDK 的简体中文应用包，
连同预编译 renderer、inputgate 工具包发布到 GitHub Releases。车机在安装时从原厂应用
生成自己的 SDK 模块，不执行应用的 Python 构建。`mgu-repo.json` 继续用于源码渠道。

## 首次使用

1. 在 fork 的 GitHub **Actions** 页面启用 workflows。
2. 选择 **Publish Chinese Store source → Run workflow → main**。后续 `main` 上
   `apps/`、`kit/`、`mgu-repo.json` 或发布 workflow 的变更会自动触发构建和发布。
3. 等待 `build`、`publish` 两个 job 成功，确认 Releases 中出现 `catalog` 和
   `cn-1.0.<运行序号>-<尝试序号>`。
4. 在车机 Store 的 **Add source** 中添加 `yongqi3566/idrive7-apps-cn`，选择 **Releases**。

选择分支或 Tags 仍然会使用源码构建。Actions 的运行附件只用于传递构建结果，车机实际下载
GitHub Release 附件。工作流使用仓库自带的 `GITHUB_TOKEN`，发布 job 需要 `contents: write`；
如果组织策略禁止写入，需在仓库设置中允许。私有仓库在车机端需要能访问它的 GitHub token。

## 版本和依赖

- 本发布渠道所有应用统一使用 `1.0.<github.run_number>`。包内 manifest、RHMI 元数据和
  catalog 的版本一致；上游 `app.json` 的版本和本地化逻辑不变。
- 每次新运行都会提高应用版本，包括上游只修改代码、未调整版本的同步。重新运行同一次
  workflow 保持应用版本，使用新的尝试序号存放附件；需要车机收到更新时发起一次新运行。
- renderer、inputgate 保留各自 `component.json` 的版本及安装钩子。应用的 `requires`
  保留，catalog 同时列出两个工具；renderer 也声明 `renderer` 能力。
- 工具代码或预编译文件发生变化时，仍需提高对应工具版本。发布前检查 renderer 的源码
  指纹与已提交二进制匹配；不匹配时停止发布，需按上游 renderer 流程重新构建并提交。
- 应用 ID 与上游相同。本源不是已安装的上游源的自动替代品；切换来源前需在 Store
  中移除旧来源的同 ID 应用，再从本源安装。移除 source 本身不会卸载应用。需要两份
  应用并存时，应另行分配独立 ID。

## 发布顺序

构建和检查成功后，先把应用包、图标、工具包上传到本次独立的 draft Release；生成指向
实际附件的 catalog 前，先公开该 Release 并重新获取附件地址，避免保存草稿的
`untagged-...` 临时地址。逐个下载并校验包和图标的大小、SHA256 后，最后更新
`catalog` Release 的 `catalog.json`。
新包准备好前车机一直读取上一份 catalog。公开仓库使用浏览器下载地址，私有仓库使用
需要 token 的 Release asset API 地址。

更新 catalog 前会与已发布版本比较：拒绝回退版本，也拒绝同版本但内容不同的包，避免
旧 workflow 重跑覆盖新版本，或工具文件修改后忘记提高版本。

## 本地验证

需要 Python 3 和 Node.js，不需要提取 SDK。以下操作只在开发电脑上运行：

```sh
python kit/tools/test_publish_store.py
python kit/tools/test_fit_to_unit.py
python kit/tools/test_id8_ui.py
python kit/tools/test_zh.py
python kit/tools/publish_store.py --version 1.0.1 --repository yongqi3566/idrive7-apps-cn
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
