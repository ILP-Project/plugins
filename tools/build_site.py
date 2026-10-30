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

import base64
import hashlib
import html
import json
import os
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
SUPPORTED_ABI = "0.2.0"


def fail(msg: str) -> None:
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(1)


def load_toml(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def plugin_dirs() -> list[Path]:
    """含 plugin.toml 的子目录即为插件。顺序稳定，让产物可复现。"""
    return sorted(p.parent for p in ROOT.glob("*/plugin.toml"))


# 与 WIT 里 `enum permission` 的成员一一对应。写死在这里是有意的：
# WIT 增删权限时这里必须一起改，否则新权限会绕过审核直接发布。
KNOWN_PERMISSIONS = {
    "http",
    "clock",
    "storage-read",
    "storage-write",
    "secret-read",
    "secret-write",
}

# 允许的许可证。不是法律审查，只是拒绝"未声明"和明显的非自由许可——
# 别人的插件里塞一段 GPL 代码进 MIT 仓库，出了问题没人能追。
ALLOWED_LICENSES = {"MIT", "Apache-2.0", "GPL-3.0", "LGPL-3.0", "MPL-2.0", "BSD-3-Clause"}


def read_meta(d: Path) -> tuple[dict, dict]:
    meta = load_toml(d / "plugin.toml")
    cargo = load_toml(d / "Cargo.toml")["package"]
    for key in (
        "name",
        "site_id",
        "site_name",
        "site_domain",
        "description",
        "author",
        "license",
        "data_source",
        "authorization_boundary",
        "maintenance",
        "known_limitations",
        "abi",
    ):
        if not meta.get(key):
            fail(f"{d.name}/plugin.toml 缺少 {key}")
    if meta["maintenance"] not in {"active", "limited", "unmaintained", "archived"}:
        fail(f"{d.name}/plugin.toml maintenance 状态无效: {meta['maintenance']}")
    if not isinstance(meta["known_limitations"], list) or not all(meta["known_limitations"]):
        fail(f"{d.name}/plugin.toml known_limitations 必须是非空字符串列表")
    # name 决定产物文件名和站点路径，必须与目录名一致，否则构建结果与直觉不符。
    if meta["name"] != d.name:
        fail(f"{d.name}/plugin.toml 的 name={meta['name']} 与目录名不一致")

    if meta["license"] not in ALLOWED_LICENSES:
        fail(
            f"{d.name}/plugin.toml license={meta['license']} 不在允许列表内。\n"
            f"允许: {', '.join(sorted(ALLOWED_LICENSES))}"
        )

    # Cargo.toml 的 license 与 plugin.toml 不一致时，产物里那份才是法务上有效的那份。
    cargo_license = cargo.get("license")
    if cargo_license != meta["license"]:
        fail(
            f"{d.name}: Cargo.toml license={cargo_license!r} 与 plugin.toml "
            f"license={meta['license']!r} 不一致"
        )

    permissions = meta.get("permissions")
    if not isinstance(permissions, list) or not permissions:
        fail(f"{d.name}/plugin.toml 必须声明 permissions（非空列表）")
    if len(set(permissions)) != len(permissions):
        fail(f"{d.name}/plugin.toml permissions 有重复项")
    unknown = sorted(set(permissions) - KNOWN_PERMISSIONS)
    if unknown:
        fail(
            f"{d.name}/plugin.toml 声明了未知权限 {unknown}。\n"
            f"若 WIT 新增了权限，请同步 build_site.py 的 KNOWN_PERMISSIONS。"
        )
    if "http" in permissions:
        domains = meta.get("allowed_domains")
        if not isinstance(domains, list) or not domains:
            fail(f"{d.name}/plugin.toml 申请了 http，必须声明 allowed_domains")
    else:
        domains = meta.get("allowed_domains", [])
        if domains:
            fail(f"{d.name}/plugin.toml 声明了 allowed_domains 但未申请 http 权限")
    for domain in domains:
        if not isinstance(domain, str) or not domain or "/" in domain or ":" in domain:
            fail(f"{d.name}/plugin.toml allowed_domains 里 {domain!r} 不是纯域名")
        # 通配会让"声明了哪些域名"变成一句空话。
        if "*" in domain:
            fail(f"{d.name}/plugin.toml allowed_domains 不允许通配: {domain}")
    # 站点主域名必须在允许列表里，否则插件连自己的站点都访问不了，
    # 这是明显的配置错误，不该等到运行时才由 host 报 permission-denied。
    site_domain = meta["site_domain"]
    if domains and not any(site_domain == d or site_domain.endswith("." + d) for d in domains):
        fail(
            f"{d.name}/plugin.toml site_domain={site_domain} 未被 allowed_domains {domains} 覆盖"
        )

    # 宿主兼容范围。ABI 只锁接口形状，管不了燃料预算、限流、落盘语义这些
    # 行为差异；没有这两个字段，一次宿主升级可以让插件静默失效。
    min_host = meta.get("min_host_version")
    max_host = meta.get("max_host_version")
    if not min_host or not max_host:
        fail(f"{d.name}/plugin.toml 必须声明 min_host_version 与 max_host_version")
    for label, value in (("min_host_version", min_host), ("max_host_version", max_host)):
        if not version_key_valid(value):
            fail(f"{d.name}/plugin.toml {label}={value!r} 不是合法的 x.y.z 版本")
    if version_key(min_host) > version_key(max_host):
        fail(
            f"{d.name}/plugin.toml min_host_version={min_host} > "
            f"max_host_version={max_host}"
        )

    meta["permissions"] = sorted(permissions)
    meta["allowed_domains"] = sorted(domains)
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


def signing_keys() -> list[tuple[str, str]]:
    """读取签名密钥，支持多把。

    格式：ILP_SIGNING_KEYS="key_id:base64,key_id:base64"。
    兼容旧的单密钥变量 ILP_SIGNING_KEY + ILP_SIGNING_KEY_ID。
    多签的价值在于：一把密钥泄露或运维离职时，不必把已有产物全部重发。
    """
    bundle = os.environ.get("ILP_SIGNING_KEYS")
    if bundle:
        keys = []
        for item in bundle.split(","):
            item = item.strip()
            if not item:
                continue
            key_id, sep, key = item.partition(":")
            if not sep or not key_id or not key:
                fail("ILP_SIGNING_KEYS 每项必须是 key_id:base64")
            keys.append((key_id.strip(), key.strip()))
        if not keys:
            fail("ILP_SIGNING_KEYS 为空")
        return keys
    key = os.environ.get("ILP_SIGNING_KEY")
    key_id = os.environ.get("ILP_SIGNING_KEY_ID")
    if not key or not key_id:
        return []
    return [(key_id, key)]


def sign(path: Path) -> list[dict]:
    """使用 Ed25519 私钥签名，返回签名列表（按 key_id 排序，保证可复现）。"""
    keys = signing_keys()
    if not keys:
        return []
    try:
        from cryptography.hazmat.primitives import serialization  # noqa: F401
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ModuleNotFoundError as exc:
        fail(f"签名需要 cryptography 包: {exc}")

    signatures = []
    seen: set[str] = set()
    for key_id, key in sorted(keys):
        if key_id in seen:
            fail(f"签名密钥 ID 重复: {key_id}")
        seen.add(key_id)
        try:
            raw = base64.b64decode(key, validate=True)
            if len(raw) != 32:
                fail(f"签名私钥 {key_id} 必须是 32 字节")
            private = Ed25519PrivateKey.from_private_bytes(raw)
            signature = private.sign(path.read_bytes())
        except Exception as exc:
            fail(f"插件签名失败（{key_id}）: {exc}")
        signatures.append(
            {
                "key_id": key_id,
                "signature": base64.b64encode(signature).decode("ascii"),
            }
        )
    return signatures


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_key_valid(v: str) -> bool:
    """只接受 x.y.z（可带 -prerelease）。范围比较靠补零对齐，多段会算错。"""
    if not isinstance(v, str):
        return False
    core = v.split("-")[0]
    parts = core.split(".")
    return len(parts) == 3 and all(p.isdigit() for p in parts)


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


def encode_signatures(signatures: list[dict]) -> dict | list[dict]:
    """单签发对象、多签发数组。

    宿主两种都接受；保持单签对象形态是为了让旧的镜像与缓存
    在只读消费方眼里仍像是同一份 schema。
    """
    return signatures[0] if len(signatures) == 1 else signatures


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
  <dt>数据来源</dt><dd>{e(meta.get('data_source', '—'))}</dd>
  <dt>授权边界</dt><dd>{e(meta.get('authorization_boundary', '—'))}</dd>
  <dt>维护状态</dt><dd><code>{e(meta.get('maintenance', '—'))}</code></dd>
  <dt>申请的权限</dt><dd>{e(', '.join(meta.get('permissions', [])) or '—')}</dd>
  <dt>访问域名</dt><dd>{e(', '.join(meta.get('allowed_domains', [])) or '不限')}</dd>
  <dt>宿主版本</dt><dd>{e(meta['min_host_version'])} – {e(meta['max_host_version'])}</dd>
  <dt>已知限制</dt><dd>{e('; '.join(meta.get('known_limitations', [])))}</dd>
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
        artifacts = {}
        for published_version in versions:
            published = SITE / meta["name"] / published_version / f"{meta['name']}.wasm"
            artifact = {
                "url": f"{meta['name']}/{published_version}/{meta['name']}.wasm",
                "size": published.stat().st_size,
                "sha256": sha256(published),
            }
            signed = sign(published)
            if signed:
                artifact["signatures"] = signed
            artifacts[published_version] = artifact
        info = {
            "name": meta["name"],
            "site_id": meta["site_id"],
            "site_name": meta["site_name"],
            "site_domain": meta["site_domain"],
            "description": meta["description"],
            "author": meta["author"],
            "license": meta["license"],
            "data_source": meta["data_source"],
            "authorization_boundary": meta["authorization_boundary"],
            "maintenance": meta["maintenance"],
            "known_limitations": meta["known_limitations"],
            "permissions": meta["permissions"],
            "allowed_domains": meta["allowed_domains"],
            "min_host_version": meta["min_host_version"],
            "max_host_version": meta["max_host_version"],
            "abi": meta["abi"],
            "latest": newest,
            "versions": versions,
            "url": artifacts[version]["url"],
            "size": artifacts[version]["size"],
            "sha256": artifacts[version]["sha256"],
            "artifacts": artifacts,
            "signatures": {
                version: encode_signatures(artifacts[version]["signatures"])
                for version in artifacts
                if artifacts[version].get("signatures")
            },
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

    registry = SITE / "registry.json"
    registry.write_text(
        json.dumps({"schema": SCHEMA, "plugins": plugins}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    # registry 自身也要签：产物签名只证明“这段代码可信”，不证明
    # “install <name> 该指向它”。签名独立成文件，避免自指与
    # 序列化规范化带来的两侧不一致。
    registry_sig = sign(registry)
    sig_path = SITE / "registry.json.sig"
    if registry_sig:
        sig_path.write_text(
            json.dumps(encode_signatures(registry_sig), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    else:
        # 没有密钥时必须清掉旧签名：留着会发布一份和当前 registry 不匹配的
        # 签名，校验失败比“没有签名”更难诊断。
        sig_path.unlink(missing_ok=True)
        print("提示: 未配置签名密钥，registry.json.sig 未生成")
    (SITE / "index.html").write_text(index_page(plugins), encoding="utf-8")
    print(f"完成: {len(plugins)} 个插件 → {SITE}")


if __name__ == "__main__":
    main()
