# 标签清理

一个 MyBooks Toolbox 外部工具，由 `mytool init` 生成。

## 目录结构

```
tags_cleaner/
├── manifest.json       # 工具元数据，见下方"字段说明"
├── .toolbuilder.json    # 构建配置（前端如果用自己的打包工具，在这里声明）
├── backend/
│   └── tool.py          # manifest.entry_backend 指向的模块，继承 BaseTool
└── frontend/
    └── index.html        # manifest.entry_frontend 指向的入口页面，自包含静态站点
```

## 本地开发

`backend/` 里的代码依赖真实的 MyBooks 运行环境（Calibre、数据库等），无法脱离 MyBooks 单独
跑；`frontend/` 是自包含的静态 HTML/JS，理论上可以用任意静态服务器单独预览页面结构，但
`bridge.fetch(...)` 调用后端的部分同样需要一个真实的 MyBooks 实例。

推荐流程：

1. 改代码。
2. `mytool validate .` —— 只校验 manifest.json 和文件结构，不打包，改完先跑一遍。
3. `mytool build` —— 打包成 `dist/tags_cleaner-<revision>.zip`，会打印 sha256。
4. 在一个开启了"开发者模式"（`ENABLE_TOOLBOX_DEV_MODE`）的 MyBooks 实例的
   `/admin/toolbox` 页面里，把这个 zip 上传安装，重启 MyBooks 进程后生效，然后在
   `/toolbox/tags_cleaner` 里实际跑起来。

## manifest.json 字段说明

| 字段 | 必填 | 说明 |
|---|---|---|
| `tool_id` | 是 | 唯一标识，只能是小写字母/数字/下划线 |
| `name` / `description` | 是 | 展示名称与描述 |
| `revision` | 是 | 语义化版本号 x.y.z |
| `author` | 是 | 作者 |
| `repo_url` | 是 | 源码仓库地址，商店审核用 |
| `core_api_version` | 是 | 依赖的最低 Core API 版本 |
| `entry_backend` | 是 | `<module>.<ClassName>`，指向 `backend/` 下继承 `BaseTool` 的类 |
| `entry_frontend` | 否 | 前端入口文件（相对 `frontend/`），缺省则工具没有 UI |
| `page` | 否 | 落地页路径，缺省用 `tool_id` |
| `api_routes` | 否 | 自定义后端接口，`[{"path": "...", "handler": "<module>.<ClassName>"}]` |

