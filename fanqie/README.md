# plugin-fanqie

番茄小说（fanqienovel.com）的 ILP 插件，编译为 WebAssembly 组件。

由宿主 [ILP](https://github.com/ILP-Project/ILP) 加载。ABI 定义在
`ilp-plugin-wit`（宿主仓库的 `crates/ilp-plugin-wit`），本仓库不复制 WIT 文件。

## 构建

```bash
rustup target add wasm32-wasip2
cargo build --target wasm32-wasip2 --release
```

产物：`target/wasm32-wasip2/release/ilp_plugin_fanqie.wasm`

安装到宿主：

```bash
cp target/wasm32-wasip2/release/ilp_plugin_fanqie.wasm <宿主>/plugins/fanqie.wasm
```

## 覆盖范围

已登录可读的**免费章节**。锁定章节服务端只返回一段试读，无法绕过。

## 依赖 ABI

本插件依赖 `ilp-plugin-wit`。在独立仓库里通过相对路径指向本地宿主仓库：

```toml
ilp-plugin-wit = { path = "../ILP/crates/ilp-plugin-wit" }
```

发布后应改为版本依赖。

## 许可与致谢

GPL-3.0。协议细节（设备注册、Argus 签名、密钥协商、正文解密）的实现参考了
[fanqie-assistant](https://github.com/fanqie-assistant) 公开的接口分析。本仓库为独立
实现的 Rust 代码，仅沿用其中的公开常量与字符映射表。
