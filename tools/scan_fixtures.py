#!/usr/bin/env python3
"""扫描插件仓库里的 fixture / 录像，拒绝明文凭据。

为什么需要独立一层：宿主录制时会做脱敏，但**手写**的 fixture 不会。
插件作者常常是"抓个包、粘进 JSON"，而抓包里一定有 Cookie。
等它进了 git，凭据就已经泄漏了——扫得再快也只是事后通知。

检查范围只看"可能含网络流量"的文件，不去扫源码：
`.json` / `.jsonl` 全部扫，`.txt` / `.log` 只在这些目录里出现时扫。
源码里出现 `"Cookie"` 字符串是正常的（插件要设置这个头）。

退出码：0 干净，1 发现问题，2 用法错误。CI 直接复用。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 出现即视为泄漏的值形态。宁可误报也不能漏。
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("Cookie 头", re.compile(r"(?i)\bcookie\b\s*[:=]\s*\S{8,}")),
    ("Set-Cookie 头", re.compile(r"(?i)set-cookie\s*:\s*\S{8,}")),
    ("Authorization 头", re.compile(r"(?i)authorization\b\s*[:=]\s*\S{8,}")),
    ("Bearer token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}")),
    # 番茄 / 巨量系的会话票据
    ("passport_csrf_token", re.compile(r"passport_csrf_token=[A-Za-z0-9]{8,}")),
    ("sessionid", re.compile(r"(?i)sessionid=[A-Za-z0-9]{8,}")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    # 私钥：Ed25519/minisign 都是 base64，但 PEM 头很容易漏
    ("PEM 私钥", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "签名密钥配置",
        re.compile(r"(?i)ilp_signing_keys?\s*[:=]\s*\S{8,}"),
    ),
]

# 这些字段名出现在 JSON 里且值不是占位符，就是泄漏。
SENSITIVE_KEYS = {
    "cookie",
    "set-cookie",
    "authorization",
    "x-tt-token",
    "x-ms-token",
    "access_token",
    "refresh_token",
    "password",
    "passwd",
    "secret",
    "signing_key",
    "private_key",
}

REDACTED_MARKERS = {"<redacted>", "redacted", "", "xxx", "***"}

# 只扫这些扩展名，避免把 wasm/图片当文本读。
TEXT_SUFFIXES = {".json", ".jsonl", ".txt", ".log", ".har", ".wit"}

# 不扫的目录。
SKIP_DIRS = {"target", "_site", "node_modules", ".git", ".github"}


def is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEYS)


def scan_json_value(value: object, path: str, findings: list[str]) -> None:
    """递归检查 JSON，按字段名判断，而不是按文本形态。

    文本正则会把 `"cookie": "<redacted>"` 也报出来；字段名 + 值双重判断
    才能既拦住真凭据，又不误报已经脱敏好的录像。
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if is_sensitive_key(str(key)) and isinstance(item, str):
                if item.lower() not in REDACTED_MARKERS and len(item) >= 8:
                    findings.append(f"{path}: 字段 {key!r} 含明文值（长度 {len(item)}）")
            scan_json_value(item, path, findings)
    elif isinstance(value, list):
        for item in value:
            scan_json_value(item, path, findings)


def scan_file(path: Path, root: Path) -> list[str]:
    findings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        # 二进制 fixture 不该存在；base64 已经把二进制挡在 JSON 里了。
        return [f"{path.relative_to(root)}: 非 UTF-8 文本，fixture 必须是纯文本"]

    for name, pattern in PATTERNS:
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            findings.append(
                f"{path.relative_to(root)}:{line}: 疑似{name}（{match.group(0)[:24]}…）"
            )

    if path.suffix == ".json":
        try:
            scan_json_value(json.loads(text), str(path.relative_to(root)), findings)
        except json.JSONDecodeError as exc:
            findings.append(f"{path.relative_to(root)}: JSON 解析失败: {exc}")
    elif path.suffix == ".jsonl":
        for lineno, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            try:
                scan_json_value(
                    json.loads(line), f"{path.relative_to(root)}:{lineno}", findings
                )
            except json.JSONDecodeError as exc:
                findings.append(f"{path.relative_to(root)}:{lineno}: JSON 解析失败: {exc}")
    return findings


def collect(root: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            files.append(path)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描插件 fixture 中的明文凭据")
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="要扫描的目录（默认当前目录，即插件仓库根）",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列出将被扫描的文件，不检查",
    )
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"错误: {root} 不是目录", file=sys.stderr)
        return 2

    files = collect(root)
    if args.list:
        for path in files:
            print(path.relative_to(root))
        return 0

    findings: list[str] = []
    for path in files:
        findings.extend(scan_file(path, root))

    if findings:
        print("发现疑似明文凭据：", file=sys.stderr)
        for item in findings:
            print(f"  {item}", file=sys.stderr)
        print(
            "\nfixture 必须脱敏：Cookie/Token/设备 ID/签名密钥都不能进仓库。\n"
            "真实的抓包请用 `ILP_RECORD_DIR=... ilp get ...` 录制，宿主会自动脱敏。",
            file=sys.stderr,
        )
        return 1

    print(f"扫描 {len(files)} 个文件，未发现明文凭据")
    return 0


if __name__ == "__main__":
    sys.exit(main())
