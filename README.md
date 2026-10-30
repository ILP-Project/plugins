# ILP plugins

ILP 的全部站点插件，以及它们的静态 registry 与站点。

**宿主在 [`ILP-Project/ILP`](https://github.com/ILP-Project/ILP)。** 本仓库只放插件，
不含宿主代码。

## 安装插件

```bash
ilp plugin install fanqie            # 最新版
ilp plugin install fanqie@1.0.0      # 指定版本
```

宿主从 `https://ilp.elysola.org/registry.json` 解析下载地址，并在安装前校验
ABI、SHA-256 与 Ed25519 签名。签名分两层：

- `registry.json` 里每个版本的 `signatures`：证明产物出自可信发布者；
- 旁路文件 `registry.json.sig`：证明元数据本身没有被改写（否则 `latest`
  可以被指向某个有效签名但有漏洞的旧版本）。

`registry.json.sig` 签的是该文件的原始字节，因此**任何会改变字节的改动
（包括格式化）都必须重新签署**——它由 `build_site.py` 在写完 registry 后
自动生成，不要手工编辑。站点由 `publish.yml` 推到 `site` 分支，
Cloudflare Pages 从该分支发布。

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
ilp-plugin-wit = { git = "https://github.com/ILP-Project/ILP", tag = "abi-v0.2.0" }
```

本地改 WIT 相关代码时，用 `.cargo/config.toml` 把它补丁到相邻的宿主 checkout，
避免每次都要推远端（该文件已 gitignore）：

```toml
[patch."https://github.com/ILP-Project/ILP"]
ilp-plugin-wit = { path = "../ILP/crates/ilp-plugin-wit" }
```

注意 `[patch]` 不能凭空造出远端：Cargo 仍会先解析 `tag = "abi-v0.2.0"`，
所以宿主仓库必须真的打过这个 tag。

## 发布

改完代码**先升 `Cargo.toml` 的 `version`**。版本号是不可变契约：
`build_site.py` 发现已发布的同版本产物内容不同会直接失败，不会覆盖。

push 到 `main` 后，`publish.yml` 会：

1. 把 `site` 分支上**上次发布的产物回填进 `_site`**；
2. 构建全部插件的 wasm；
3. 校验 `plugin.toml` 的 `abi` 等于 `SUPPORTED_ABI`；
4. 生成 `_site/registry.json`、`registry.json.sig`、每个版本的 `latest.json` 和站点页面；
5. 校验每个已声明版本的产品都在、摘要都对，registry 自身的签名也在；
6. 把 `_site` 推到 `site` 分支，Cloudflare Pages 从该分支发布。

第 1 步不能省。版本不可变守卫靠比较 `_site` 里已有产物的 sha256，
而 CI 每次都是干净目录——不回填的话守卫永远不触发，改代码不升版本
就会静默覆盖别人已装好的同名版本。

### 为什么不在 Cloudflare Pages 里构建

CF 的构建镜像**不预装 Rust**（官方文档把 Rust 和 Swift 列为"默认不安装"）。
在那里构建要么每次现装 rustup，要么放弃 wasm 构建。GitHub Actions 镜像
自带工具链，缓存也好做，所以构建放 CI、CF 只当静态托管。

这也意味着**不需要 `CLOUDFLARE_API_TOKEN`**：只推分支，不调 API。

## 本地开发

```bash
rustup target add wasm32-wasip2
cargo test
cargo build --target wasm32-wasip2 --release
python3 tools/build_site.py     # 生成 _site/ 供本地检查
```

## 新增一个站点插件

1. 新建 `mysite/`，写 `Cargo.toml` 与 `plugin.toml`。
2. 在根 `Cargo.toml` 的 `members` 里加上 `"mysite"`。
3. `python3 tools/build_site.py` 本地验证。

`plugin.toml` 的 `site_id` 必须全局唯一——宿主按它找插件，重复会让行为
取决于文件加载顺序。构建脚本会拒绝重复项。

## 许可

各插件见其 `plugin.toml` 的 `license` 字段（当前均为 GPL-3.0）。
