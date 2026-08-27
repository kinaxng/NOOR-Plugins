#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

VALID_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")

TYPE_CAPS = {
    "rss_source": ["network_outbound", "rss_fetch", "sidebar_page"],
    "downloader": ["network_outbound", "download_submit"],
    "subtitle_provider": ["subtitle_search"],
    "dashboard_widget": ["dashboard_widget"],
    "tool": ["sidebar_page"],
}


def write(path: Path, text: str, force: bool) -> None:
    if path.exists() and not force:
        raise SystemExit(f"Refusing to overwrite existing file: {path}. Use --force.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def plugin_name(plugin_id: str) -> str:
    return " ".join(part.capitalize() for part in plugin_id.split("-"))


def manifest(plugin_id: str, typ: str, caps: list[str], name: str) -> dict:
    data = {
        "id": plugin_id,
        "name": name,
        "version": "0.1.0",
        "type": typ,
        "description": f"{name} plugin",
        "tags": [typ],
        "capabilities": caps,
        "config_schema": {},
        "default_config": {},
    }
    if "sidebar_page" in caps:
        data["contributions"] = {"sidebar": {"label": name, "route": f"/plugins/{plugin_id}", "icon": "plugin"}}
        data["frontend"] = {"type": "module", "entry": "frontend/page.js", "style": "frontend/style.css"}
    return data


def frontend_page(plugin_id: str, name: str) -> str:
    return f'''export async function mount(el, sdk = {{}}) {{
  const pluginId = sdk.pluginId || '{plugin_id}'
  const api = (path, init) => sdk.api?.plugin ? sdk.api.plugin(path, init) : fetch(`/api/plugins/${{pluginId}}${{path}}`, init)
  const state = {{ loading: false, error: '', items: [] }}

  async function load() {{
    state.loading = true
    state.error = ''
    render()
    try {{
      const res = await api('/actions/status', {{ method: 'POST', headers: {{ 'Content-Type': 'application/json' }}, body: JSON.stringify({{ payload: {{}} }}) }})
      const data = await res.json().catch(() => ({{}}))
      if (!res.ok) throw new Error(data.detail || '加载失败')
      state.items = Array.isArray(data.items) ? data.items : []
    }} catch (error) {{
      state.error = error?.message || '加载失败'
    }} finally {{
      state.loading = false
      render()
    }}
  }}

  function render() {{
    const ui = sdk.ui
    el.innerHTML = ''
    const page = ui?.page ? ui.page({{ className: '{plugin_id}-page' }}) : document.createElement('div')
    page.className ||= '{plugin_id}-page'

    const toolbar = document.createElement('div')
    toolbar.className = '{plugin_id}-toolbar'
    const title = document.createElement('div')
    title.className = '{plugin_id}-title'
    title.textContent = '{name}'
    const refresh = ui?.button
      ? ui.button({{ label: state.loading ? '刷新中' : '刷新', tone: 'primary', disabled: state.loading, onClick: () => load() }})
      : Object.assign(document.createElement('button'), {{ textContent: state.loading ? '刷新中' : '刷新', disabled: state.loading, onclick: () => load() }})
    toolbar.append(title, refresh)
    page.appendChild(toolbar)

    if (state.loading) {{
      page.appendChild(ui?.skeletonGrid ? ui.skeletonGrid({{ count: 6 }}) : document.createTextNode('加载中...'))
    }} else if (state.error) {{
      page.appendChild(ui?.notice ? ui.notice({{ text: state.error, tone: 'error' }}) : document.createTextNode(state.error))
    }} else if (!state.items.length) {{
      page.appendChild(ui?.emptyState ? ui.emptyState({{ text: '暂无内容' }}) : document.createTextNode('暂无内容'))
    }} else {{
      const grid = document.createElement('div')
      grid.className = '{plugin_id}-grid'
      for (const item of state.items) {{
        const card = ui?.card ? ui.card({{ className: '{plugin_id}-card' }}) : document.createElement('div')
        card.className ||= '{plugin_id}-card'
        card.textContent = String(item.name || item.title || item.id || '-')
        grid.appendChild(card)
      }}
      page.appendChild(grid)
    }}

    el.appendChild(page)
  }}

  await load()
  return () => {{ el.innerHTML = '' }}
}}
'''


def frontend_style(plugin_id: str) -> str:
    return f'''.{plugin_id}-page{{display:flex;flex-direction:column;gap:var(--space-3,12px);font-family:var(--font-display)}}
.{plugin_id}-toolbar{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}}
.{plugin_id}-title{{color:var(--color-text-primary);font-size:var(--font-size-lg);font-weight:var(--font-weight-bold)}}
.{plugin_id}-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}}
.{plugin_id}-card{{min-height:92px;padding:12px;color:var(--color-text-secondary)}}
'''


def backend_py(plugin_id: str) -> str:
    return f'''from __future__ import annotations


def actions():
    return {{
        "status": status,
    }}


async def status(payload=None, config=None, context=None):
    return {{"ok": True, "items": []}}
'''


def readme(plugin_id: str, name: str) -> str:
    return f'''# {name}

Generated NOOR plugin scaffold.

## Validate

```bash
scripts/noor-plugin validate plugins/{plugin_id}
```
'''


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="noor-plugin create")
    parser.add_argument("plugin_id")
    parser.add_argument("--type", default="tool", choices=sorted(TYPE_CAPS))
    parser.add_argument("--cap", "--caps", dest="caps", default="", help="逗号分隔 capabilities；默认按 type 生成")
    parser.add_argument("--name", default="")
    parser.add_argument("--plugins-dir", default="plugins")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    plugin_id = args.plugin_id.strip()
    if not VALID_ID.fullmatch(plugin_id):
        print("NOOR_PLUGIN_ERROR PLUGIN_ID_INVALID id 只能使用小写字母、数字和连字符", file=sys.stderr)
        return 2
    caps = [x.strip() for x in args.caps.split(',') if x.strip()] or TYPE_CAPS[args.type]
    name = args.name.strip() or plugin_name(plugin_id)
    root = Path(args.plugins_dir) / plugin_id
    if root.exists() and any(root.iterdir()) and not args.force:
        print(f"NOOR_PLUGIN_ERROR TARGET_EXISTS {root} already exists; use --force", file=sys.stderr)
        return 2

    data = manifest(plugin_id, args.type, caps, name)
    write(root / "plugin.json", json.dumps(data, ensure_ascii=False, indent=2) + "\n", args.force)
    write(root / "backend.py", backend_py(plugin_id), args.force)
    if "sidebar_page" in caps:
        write(root / "frontend/page.js", frontend_page(plugin_id, name), args.force)
        write(root / "frontend/style.css", frontend_style(plugin_id), args.force)
    write(root / "README.md", readme(plugin_id, name), args.force)
    print(f"NOOR_PLUGIN_CREATED {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
