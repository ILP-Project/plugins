# ILP plugins

ILP 的全部站点插件，以及它们的静态 registry 与站点。

**宿主在 [`ILP-Project/ILP`](https://github.com/ILP-Project/ILP)。** 本仓库只放插件，
不含宿主代码。

## 安装插件

```bash
ilp plugin install fanqie            # 最新版
ilp plugin install fanqie@1.0.0      # 指定版本
```

宿主从本站的 `registry.json` 解析下载地址，并在安装前校验 ABI 与 SHA-256。

## 目录

```
plugins/
├── fanqie/               一个站点一个子目录
│   ├── Cargo.toml        version 从这里读
│   ├── plugin.toml       站点元数据（人写，只在变化时改）
│   └── src/
├── tools/build_site.py   构建全部插件 → 生成 _site/
├── site/                 站点静态资源
└── .github/workflows/    只做校验，部署交给 CF Pages
```

## 本地构建

```bash
rustup target add wasm32-wasip2
cargo build --target wasm32-wasip2 --release
```

产物在 `target/wasm32-wasip2/release/`，复制到宿主的 `plugins/` 下即可。

`ilp-plugin-wit`（ABI 契约）通过 git 依赖指向宿主仓库的 tag：

```toml
ilp-plugin-wit = { git = "https://github.com/ILP-Project/ILP", tag = "abi-v0.1.0" }
```

本地改 WIT 相关代码时，用 `.cargo/config.toml` 把它补丁到相邻的宿主 checkout，
避免每次都要推远端（该文件已 gitignore）：

```toml
[patch."https://github.com/ILP-Project/ILP"]
ilp-plugin-wit = { path = "../ILP/crates/ilp-plugin-wit" }
```

注意 `[patch]` 不能凭空造出远端：Cargo 仍会先解析 `tag = "abi-v0.1.0"`，
所以宿主仓库必须真的打过这个 tag。

## 发布

改完代码**先升 `Cargo.toml` 的 `version`**。版本号是不可变契约：
`build_site.py` 发现已发布的同版本产物内容不同会直接失败，不会覆盖。

之后 push 到 `main`，Cloudflare Pages 自动：

1. 构建全部插件的 wasm；
2. 交叉校验 `plugin.toml` 的 `abi` 与 WIT 的 `package ilp:plugin@x.y.z`；
3. 生成 `_site/registry.json`、每个版本的 `latest.json` 和站点页面；
4. 部署。

## 新增一个站点插件

1. 新建 `mysite/`，写 `Cargo.toml` 与 `plugin.toml`。
2. 在根 `Cargo.toml` 的 `members` 里加上 `"mysite"`。
3. `python3 tools/build_site.py` 本地验证。

`plugin.toml` 的 `site_id` 必须全局唯一——宿主按它找插件，重复会让行为
取决于文件加载顺序。构建脚本会拒绝重复项。

## 许可

各插件见其 `plugin.toml` 的 `license` 字段（当前均为 GPL-3.0）。
