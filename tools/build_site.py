#!/usr/bin/env python3
"""构建所有插件并生成站点 + registry。

CF Pages 的构建命令就是它。设计约束：

1. **不手写任何会随发布变化的字段。** version 来自 Cargo.toml，abi 与 plugin.toml
   交叉校验，url/size/sha256 来自构建产物。旧 `plugin.json` 手写过一个 404 地址，
   这类错误只能靠"不给人写的机会"来消除。

2. **已发布版本不可变。** 若 `_site/<name>/<version>/` 里已有产物且 sha256 不同，
   直接失败。改代码必须升 version —— 否则把那台已经装了旧版机器上的
   `ilp plugin install fanqie@1.0.0` 变成不可复现的赌博。

3. **产物缺失不静默降级。** cargo build 失败就退出非零，宁可让站点发布失败，
   也不要发布一个 registry 里指向不存在文件的索引。
"""

from __future__ import annotations

import hashlib
import html
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SITE = ROOT / "_site"
TARGET = "wasm32-wasip2"
SCHEMA = 1

# 宿主支持的 ABI。必须与 ILP 仓库 crates/ilp-plugin-wit/wit/plugin.wit 里
# `package ilp:plugin@x.y.z` 一致；宿主侧的 ABI 校验常量也取同一个值。
SUPPORTED_ABI = "0.1.0"


def fail(msg: str) -> None:
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(1)


def load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def plugin_dirs() -> list[Path]:
    """含 plugin.toml 的子目录即为插件。顺序稳定，让产物可复现。"""
    return sorted(p.parent for p in ROOT.glob("*/plugin.toml"))


def read_meta(d: Path) -> tuple[dict, dict]:
    meta = load_toml(d / "plugin.toml")
    cargo = load_toml(d / "Cargo.toml")["package"]
    for key in ("name", "site_id", "site_name", "site_domain", "description", "abi"):
        if not meta.get(key):
            fail(f"{d.name}/plugin.toml 缺少 {key}")
    # name 决定产物文件名和站点路径，必须与目录名一致，否则构建结果与直觉不符。
    if meta["name"] != d.name:
        fail(f"{d.name}/plugin.toml 的 name={meta['name']} 与目录名不一致")
    return meta, cargo


def build(d: Path) -> Path:
    """构建单个插件，返回 wasm 路径。"""
    print(f"构建 {d.name} ...")
    proc = subprocess.run(
        [
            "cargo",
            "build",
            "--target",
            TARGET,
            "--release",
            "--manifest-path",
            str(d / "Cargo.toml"),
        ],
        cwd=ROOT,
        text=True,
    )
    if proc.returncode != 0:
        fail(f"{d.name} 构建失败")
    # crate 名可能带 ilp-plugin- 前缀，产物文件名以下划线形式出现。
    wasm = ROOT / "target" / TARGET / "release" / f"{crate_stem(d)}.wasm"
    if not wasm.is_file():
        fail(f"找不到构建产物 {wasm}")
    return wasm


def crate_stem(d: Path) -> str:
    return load_toml(d / "Cargo.toml")["package"]["name"].replace("-", "_")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_key(v: str) -> tuple:
    """把 `1.10.0` 这类版本变成可比较的元组。

    直接用字符串排序会把 1.10.0 排在 1.9.0 前面——版本越多越容易撞上，
    而"最新版本"排错会让站点上的安装指引指向一个旧版本。
    """
    parts = []
    for piece in v.split("-")[0].split("."):
        parts.append(int(piece) if piece.isdigit() else 0)
    # 预发布版本（1.0.0-rc1）排在同号的正式版之前。
    return (tuple(parts), 1 if "-" in v else 0)


def existing_versions(name: str) -> list[str]:
    """扫 _site 里已发布的版本目录。"""
    base = SITE / name
    if not base.is_dir():
        return []
    found = [
        p.name for p in base.iterdir() if p.is_dir() and (p / f"{name}.wasm").is_file()
    ]
    return sorted(found, key=version_key, reverse=True)


def publish(name: str, version: str, wasm: Path) -> str:
    """把产物放进 _site，返回 sha256。已发布版本内容不同则失败。"""
    dest_dir = SITE / name / version
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{name}.wasm"
    digest = sha256(wasm)
    if dest.is_file():
        old = sha256(dest)
        if old != digest:
            fail(
                f"{name} {version} 已发布过，但产物内容不同（{old[:12]}… → {digest[:12]}…）。\n"
                f"版本号是不可变契约：请升 fanqie/Cargo.toml 里的 version 后重试。"
            )
    shutil.copyfile(wasm, dest)
    return digest


def page(meta: dict, info: dict, versions: list[str]) -> str:
    e = html.escape
    rows = "\n".join(
        f"<li><code>{e(v)}</code>{' <em>(latest)</em>' if v == info['latest'] else ''}</li>"
        for v in versions
    )
    return f"""<!doctype html>
<meta charset="utf-8">
<title>{e(meta['site_name'])} — ILP 插件</title>
<style>
  body {{ font: 16px/1.6 system-ui, sans-serif; max-width: 44rem; margin: 3rem auto; padding: 0 1.2rem; color: #1c1c1e; }}
  code, pre {{ font-family: ui-monospace, monospace; background: #f2f2f4; border-radius: 4px; }}
  code {{ padding: .1em .35em; }}
  pre {{ padding: .8rem 1rem; overflow-x: auto; }}
  h1 {{ font-size: 1.7rem; margin-bottom: .2rem; }}
  .sub {{ color: #6b6b70; margin-top: 0; }}
  dt {{ font-weight: 600; margin-top: .8rem; }}
  dd {{ margin: 0 0 0 0; }}
  ul {{ padding-left: 1.4rem; }}
</style>
<h1>{e(meta['site_name'])}</h1>
<p class="sub">{e(meta['site_domain'])} · 插件 <code>{e(meta['name'])}</code></p>
<p>{e(meta['description'])}</p>

<h2>安装</h2>
<pre><code>ilp plugin install {e(meta['name'])}</code></pre>
<p>指定版本：</p>
<pre><code>ilp plugin install {e(meta['name'])}@{e(info['latest'])}</code></pre>

<h2>信息</h2>
<dl>
  <dt>最新版本</dt><dd><code>{e(info['latest'])}</code></dd>
  <dt>ABI</dt><dd><code>{e(meta['abi'])}</code></dd>
  <dt>作者</dt><dd>{e(meta.get('author', '—'))}</dd>
  <dt>许可</dt><dd>{e(meta.get('license', '—'))}</dd>
  <dt>产物大小</dt><dd>{info['size']:,} 字节</dd>
  <dt>SHA-256</dt><dd><code>{e(info['sha256'])}</code></dd>
</dl>

<h2>版本</h2>
<ul>
{rows}
</ul>

<p><a href="../">← 全部插件</a></p>
"""


def index_page(plugins: list[dict]) -> str:
    e = html.escape
    if not plugins:
        body = "<p>还没有插件。</p>"
    else:
        items = "\n".join(
            f'<li><a href="{e(p["name"])}/">{e(p["site_name"])}</a> '
            f'<code>{e(p["name"])}</code> · {e(p["latest"])} · {e(p["site_domain"])}'
            f'<br><span class="d">{e(p["description"])}</span></li>'
            for p in plugins
        )
        body = f"<ul>\n{items}\n</ul>"
    return f"""<!doctype html>
<meta charset="utf-8">
<title>ILP 插件</title>
<style>
  body {{ font: 16px/1.6 system-ui, sans-serif; max-width: 44rem; margin: 3rem auto; padding: 0 1.2rem; color: #1c1c1e; }}
  code {{ font-family: ui-monospace, monospace; background: #f2f2f4; padding: .1em .35em; border-radius: 4px; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ padding: .8rem 0; border-bottom: 1px solid #e6e6e8; }}
  .d {{ color: #6b6b70; font-size: .95em; }}
</style>
<h1>ILP 插件</h1>
<p>安装：<code>ilp plugin install &lt;name&gt;</code></p>
{body}
"""


def main() -> None:
    dirs = plugin_dirs()
    if not dirs:
        fail("没有找到任何插件（缺少 */plugin.toml）")

    SITE.mkdir(exist_ok=True)
    plugins: list[dict] = []
    seen_names: set[str] = set()
    seen_sites: set[str] = set()

    for d in dirs:
        meta, cargo = read_meta(d)

        if meta["name"] in seen_names:
            fail(f"插件名重复: {meta['name']}")
        if meta["site_id"] in seen_sites:
            fail(f"site_id 重复: {meta['site_id']}（宿主按 site_id 找插件，重复会导致行为依赖加载顺序）")
        seen_names.add(meta["name"])
        seen_sites.add(meta["site_id"])

        if meta["abi"] != SUPPORTED_ABI:
            fail(
                f"{d.name} 声明 abi={meta['abi']}，但本站点只发布 abi={SUPPORTED_ABI}。\n"
                f"若 ABI 真的升版了，请同步 build_site.py 的 SUPPORTED_ABI 和宿主的 ABI 常量。"
            )

        version = cargo["version"]
        wasm = build(d)
        digest = publish(meta["name"], version, wasm)

        # 已发布版本 + 本次版本，去重后按语义序倒排（这里用简单的数字段比较）。
        versions = sorted(set(existing_versions(meta["name"])) | {version}, key=version_key, reverse=True)
        # `latest` 必须是版本意义上的最大值，而不是"这次构建的版本"：
        # 用一个旧版本号重新构建时，站点不能把 latest 降回去。
        newest = max(versions, key=version_key)
        info = {
            "name": meta["name"],
            "site_id": meta["site_id"],
            "site_name": meta["site_name"],
            "site_domain": meta["site_domain"],
            "description": meta["description"],
            "abi": meta["abi"],
            "latest": newest,
            "versions": versions,
            "url": f"{meta['name']}/{version}/{meta['name']}.wasm",
            "size": wasm.stat().st_size,
            "sha256": digest,
        }
        plugins.append(info)

        # latest.json：让 `install <name>` 不必先拉整个 registry 也能解析
        # （宿主目前直接读 registry，保留它是为了让 CDN 缓存更细粒度）。
        (SITE / meta["name"] / "latest.json").write_text(
            json.dumps(newest, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        (SITE / meta["name"] / "index.html").write_text(
            page(meta, info, versions), encoding="utf-8"
        )
        print(f"  {meta['name']} {version}  {info['size']:,} B  {digest[:12]}…")

    # site/ 里的静态资源原样拷进产物（robots.txt 之类）。
    static = ROOT / "site"
    if static.is_dir():
        shutil.copytree(static, SITE, dirs_exist_ok=True)

    (SITE / "registry.json").write_text(
        json.dumps({"schema": SCHEMA, "plugins": plugins}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (SITE / "index.html").write_text(index_page(plugins), encoding="utf-8")
    print(f"完成: {len(plugins)} 个插件 → {SITE}")


if __name__ == "__main__":
    main()
