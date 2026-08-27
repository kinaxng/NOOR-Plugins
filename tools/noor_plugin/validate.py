#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ALLOWED_TYPES = {
    "knowledge_app",
    "rss_source",
    "downloader",
    "source",
    "subtitle_provider",
    "dashboard_widget",
    "repository",
    "tool",
}
KNOWN_CAPABILITIES = {
    "avatar_library",
    "downloader",
    "dashboard_widget",
    "download_submit",
    "external_task_provider",
    "knowledge_provider",
    "knowledge_view",
    "local_metrics",
    "network_outbound",
    "recommendation_provider",
    "remote_tasks",
    "resource_match",
    "resource_search",
    "rss_fetch",
    "sidebar_widget",
    "sidebar_page",
    "subtitle_search",
    "subtitle_search_local",
    "subscription_core",
}
FORBIDDEN_BROWSER_CALLS = ("alert(", "window.alert(", "window.confirm(", "prompt(", "window.prompt(")
FORBIDDEN_HOST_PATTERNS = (
    re.compile(r"https?://(?:localhost|127\.0\.0\.1)(?::\d+)?"),
    re.compile(r"https?://192\.168\.\d+\.\d+(?::\d+)?"),
    re.compile(r"(?<!\d)(?:localhost|127\.0\.0\.1):9898"),
)
CSS_SELECTOR_RE = re.compile(r"(^|[\s,{])\.([A-Za-z_-][\w-]*)")

@dataclass
class Issue:
    level: str
    code: str
    path: str
    message: str

    def line(self) -> str:
        return f"NOOR_PLUGIN_{self.level} {self.code} {self.path} {self.message}"


def load_json(path: Path, issues: list[Issue]) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        issues.append(Issue("ERROR", "MANIFEST_MISSING", str(path), "plugin.json 不存在"))
    except json.JSONDecodeError as exc:
        issues.append(Issue("ERROR", "MANIFEST_JSON_INVALID", str(path), f"JSON 解析失败：{exc}"))
    return {}


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root.parent))
    except ValueError:
        return str(path)


def validate_manifest(plugin_dir: Path, manifest: dict, issues: list[Issue]) -> None:
    required = ["id", "name", "version", "type", "description", "capabilities"]
    for key in required:
        if key not in manifest:
            issues.append(Issue("ERROR", "MANIFEST_FIELD_MISSING", "plugin.json", f"缺少字段 {key}"))
    plugin_id = str(manifest.get("id", ""))
    if plugin_id and plugin_id != plugin_dir.name:
        issues.append(Issue("ERROR", "PLUGIN_ID_DIR_MISMATCH", "plugin.json", f"id={plugin_id} 与目录名 {plugin_dir.name} 不一致"))
    if plugin_id and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", plugin_id):
        issues.append(Issue("ERROR", "PLUGIN_ID_INVALID", "plugin.json", "id 只能使用小写字母、数字和连字符"))
    typ = manifest.get("type")
    if typ and typ not in ALLOWED_TYPES:
        issues.append(Issue("WARN", "PLUGIN_TYPE_UNKNOWN", "plugin.json", f"未知 type：{typ}"))
    caps = manifest.get("capabilities", [])
    if not isinstance(caps, list):
        issues.append(Issue("ERROR", "CAPABILITIES_INVALID", "plugin.json", "capabilities 必须是数组"))
        caps = []
    for cap in caps:
        if cap not in KNOWN_CAPABILITIES:
            issues.append(Issue("WARN", "CAPABILITY_UNKNOWN", "plugin.json", f"未知 capability：{cap}"))
    frontend = manifest.get("frontend") or {}
    if "sidebar_page" in caps:
        if not frontend:
            issues.append(Issue("ERROR", "FRONTEND_REQUIRED", "plugin.json", "声明 sidebar_page 时必须提供 frontend"))
        else:
            entry = frontend.get("entry", "frontend/page.js")
            entry_path = plugin_dir / str(entry)
            if not entry_path.exists():
                issues.append(Issue("ERROR", "FRONTEND_ENTRY_MISSING", str(entry_path), "frontend entry 不存在"))
            style = frontend.get("style")
            if style and not (plugin_dir / str(style)).exists():
                issues.append(Issue("ERROR", "FRONTEND_STYLE_MISSING", str(plugin_dir / str(style)), "frontend style 不存在"))


def validate_frontend(plugin_dir: Path, manifest: dict, issues: list[Issue]) -> None:
    frontend = manifest.get("frontend") or {}
    entry = plugin_dir / str(frontend.get("entry", "frontend/page.js"))
    if entry.exists():
        text = entry.read_text(encoding="utf-8", errors="ignore")
        if "mount(" not in text or "export" not in text:
            issues.append(Issue("ERROR", "FRONTEND_MOUNT_MISSING", str(entry), "插件前端应导出 mount(el, sdk)"))
        for token in FORBIDDEN_BROWSER_CALLS:
            if token in text:
                issues.append(Issue("ERROR", "BROWSER_DIALOG_FORBIDDEN", str(entry), f"禁止使用原生 {token.rstrip('(')}，请使用 sdk.ui.confirm/modal"))
        for pattern in FORBIDDEN_HOST_PATTERNS:
            if pattern.search(text):
                issues.append(Issue("ERROR", "HARDCODED_NOOR_HOST", str(entry), "禁止硬编码主程序 host/port，请使用 sdk.api.fetch/plugin"))
        if "sdk.api?.plugin" not in text and ("fetch(`/api/plugins/" in text or "fetch('/api/plugins/" in text or 'fetch("/api/plugins/' in text):
            issues.append(Issue("WARN", "PLUGIN_API_SHOULD_USE_SDK", str(entry), "插件自身 API 建议使用 sdk.api.plugin()"))
        if any(name in text for name in ("mteam-modal", "qb-modal", "-modal-mask")) and "sdk.ui.modal" not in text:
            issues.append(Issue("WARN", "CUSTOM_MODAL_WITHOUT_SDK", str(entry), "检测到自定义弹窗，建议迁移 sdk.ui.modal"))


def validate_css(plugin_dir: Path, manifest: dict, issues: list[Issue]) -> None:
    plugin_id = str(manifest.get("id") or plugin_dir.name)
    prefix = plugin_id.replace("-plugin", "").replace("-", "-")
    allowed_prefixes = {prefix, "noor-plugin"}
    if plugin_id == "qbittorrent":
        allowed_prefixes.add("qb")
    if plugin_id == "av-recommend":
        allowed_prefixes.add("av-rec")
    if plugin_id == "mdc-ng-manual":
        allowed_prefixes.add("mdc")
    if plugin_id == "subscription-core":
        allowed_prefixes.add("sub")
    if plugin_id == "xunlei-remote":
        allowed_prefixes.add("xunlei")
    frontend = manifest.get("frontend") or {}
    css_files = []
    style = frontend.get("style")
    if style:
        css_files.append(plugin_dir / str(style))
    css_files.extend((plugin_dir / "frontend").glob("*.css"))
    seen = set()
    for css in css_files:
        if not css.exists() or css in seen:
            continue
        seen.add(css)
        text = css.read_text(encoding="utf-8", errors="ignore")
        if "var(--color-" not in text and "var(--radius" not in text:
            issues.append(Issue("WARN", "DESIGN_TOKEN_MISSING", str(css), "CSS 应优先使用 NOOR design token"))
        classes = {m.group(2) for m in CSS_SELECTOR_RE.finditer(text)}
        for cls in sorted(classes):
            if cls.startswith(("is-", "has-")):
                continue
            if not any(cls.startswith(p + "-") or cls.startswith(p + "__") or cls == p for p in allowed_prefixes):
                issues.append(Issue("WARN", "CSS_PREFIX_MISSING", str(css), f".{cls} 未使用插件前缀或 noor-plugin 前缀"))
                break


def validate_backend(plugin_dir: Path, manifest: dict, issues: list[Issue]) -> None:
    backend = plugin_dir / "backend.py"
    caps = set(manifest.get("capabilities") or [])
    if caps & {"rss_fetch", "subtitle_search", "download_submit", "dashboard_widget"} and not backend.exists():
        issues.append(Issue("WARN", "BACKEND_MISSING", str(backend), "插件声明了后端能力但 backend.py 不存在"))
    if backend.exists():
        text = backend.read_text(encoding="utf-8", errors="ignore")
        if "Plugin" not in text and "def handle" not in text and "actions" not in text:
            issues.append(Issue("WARN", "BACKEND_HANDLER_UNCLEAR", str(backend), "未检测到明显插件 handler/action 结构"))


def validate(plugin_dir: Path) -> list[Issue]:
    issues: list[Issue] = []
    manifest_path = plugin_dir / "plugin.json"
    manifest = load_json(manifest_path, issues)
    if manifest:
        validate_manifest(plugin_dir, manifest, issues)
        validate_frontend(plugin_dir, manifest, issues)
        validate_css(plugin_dir, manifest, issues)
        validate_backend(plugin_dir, manifest, issues)
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="noor-plugin validate")
    parser.add_argument("paths", nargs="+", help="插件目录，或 plugins 目录")
    parser.add_argument("--strict", action="store_true", help="将 WARN 也视为失败")
    args = parser.parse_args(argv)

    all_issues: list[Issue] = []
    targets: list[Path] = []
    for raw in args.paths:
        path = Path(raw).resolve()
        if (path / "plugin.json").exists():
            targets.append(path)
        else:
            targets.extend(sorted(p.parent for p in path.glob("*/plugin.json")))
    if not targets:
        print("NOOR_PLUGIN_ERROR NO_PLUGIN_FOUND - 未找到 plugin.json", file=sys.stderr)
        return 2
    for target in targets:
        issues = validate(target)
        all_issues.extend(issues)
        if not issues:
            print(f"NOOR_PLUGIN_OK {target}")
        else:
            for issue in issues:
                print(issue.line())
    has_error = any(i.level == "ERROR" for i in all_issues)
    has_warn = any(i.level == "WARN" for i in all_issues)
    return 1 if has_error or (args.strict and has_warn) else 0

if __name__ == "__main__":
    raise SystemExit(main())
