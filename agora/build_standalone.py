#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_standalone.py — 生成单文件版 Agora 看板 index.standalone.html

用途：把 css / js / 数据全部内联进一个 HTML 文件，
使其可以在 GitHub Pages 上只上传「一个文件」即可运行，
也支持直接双击用 file:// 打开（无需 HTTP 服务器）。

数据源与 build_bundle.py 一致（results/ 为唯一事实来源）。
用法：  python3 build_standalone.py
输出：  ./index.standalone.html
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def read(p):
    with open(os.path.join(HERE, p), "r", encoding="utf-8") as f:
        return f.read()


def main():
    index = read("index.html")
    css = read("css/style.css")
    md = read("js/markdown.js")
    app = read("js/app.js")
    config = read("config.json")
    bundle = read("data/bundle.json")

    cfg_obj = json.loads(config)
    # 单文件版常为“未配置仓库”状态：占位 raw_base 会产生死链，清空以免误导
    if "OWNER" in (cfg_obj.get("raw_base") or ""):
        cfg_obj["raw_base"] = ""

    embedded = (
        "<script>\n"
        "window.__AGORA_EMBEDDED__ = "
        + json.dumps(
            {"config": cfg_obj, "bundle": json.loads(bundle)},
            ensure_ascii=False,
        )
        + ";\n</script>"
    )

    out = index
    out = out.replace(
        '<link rel="stylesheet" href="./css/style.css" />',
        "<style>\n" + css + "\n</style>",
    )
    # markdown 渲染器必须在 app.js 之前
    out = out.replace(
        '<script src="./js/markdown.js"></script>',
        embedded + "\n<script>\n" + md + "\n</script>",
    )
    out = out.replace(
        '<script src="./js/app.js"></script>',
        "<script>\n" + app + "\n</script>",
    )

    # 安全校验：不应再残留外部资源引用
    for leftover in ['src="./', 'href="./css']:
        if leftover in out:
            raise SystemExit(f"[WARN] 仍残留外部引用: {leftover}")

    out_path = os.path.join(HERE, "index.standalone.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    print(f"已生成单文件看板：{out_path}")
    print(f"体积：{os.path.getsize(out_path)/1024:.1f} KB")


if __name__ == "__main__":
    main()
