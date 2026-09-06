# Noto 简体中文界面字体

上游：[notofonts/noto-cjk](https://github.com/notofonts/noto-cjk)，固定提交 `f8d157532fbfaeda587e826d4cd5b21a49186f7c`。文件未修改，来源路径与 SHA-256 见 `provenance.json`。

- `NotoSansSC-Medium.otf`：正文与数据，真实 Medium 字重（500）。
- `NotoSansSC-Bold.otf`：标题、按钮和富文本强调，真实 Bold 字重（700）。
- `NotoSansMonoCJKsc-Regular.otf`：代码编辑器等宽字体，支持中文注释。

字体采用 SIL Open Font License 1.1，原始许可保留于 `LICENSE`，版权信息同时保留在原始字体的 name 表中。分发时保留字体、版权信息和许可证；无需在玩家电脑安装字体。

统一字体和控件属性在 `resources/ui/art_v2/theme.tres`；它由 `scripts/import_art_v2.py` 重建。项目默认字体沿用 `resources/ui/chinese_system_font.tres` 路径，该资源现在包装随项目分发的 Medium 字体，不再依赖操作系统选择中文正文字体。
