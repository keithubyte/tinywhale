# vendor/

- `pow_worker.js`：DeepSeek 网页版自带的 PoW solver（`static/76608.<hash>.js`），
  从 `fe-static.deepseek.com` 取得，仅用于在隐藏的 WebKit 页面里计算
  `x-ds-pow-response`。它不是本项目代码；版权归 DeepSeek 所有，仅作本地个人使用。
- `typelib/`：Ubuntu `gir1.2-webkit-6.0` / `gir1.2-javascriptcoregtk-6.0` 的
  GObject Introspection 数据，系统缺失时由 `run.sh` 通过 `GI_TYPELIB_PATH` 使用。
- `node/node`：Node.js v22.14.0 Linux x86_64 运行时（MIT 许可，见 `node/LICENSE`），
  仅用于本地启动 MathJax 公式渲染器；其他架构会自动回退到内置的 Pango 公式排版。
- `mathjax-node/`：MathJax 3.2.2（Apache-2.0，见 `mathjax-node/LICENSE`）与
  `render.js` 小工具。它把行间公式转成自包含 SVG，再由客户端转成图片显示，
  不联网、不执行用户 HTML。
