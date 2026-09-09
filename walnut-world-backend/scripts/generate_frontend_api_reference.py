"""Generate the Demo HTTP reference from mounted routes and local wire schemas.

No database or provider connection is opened. Examples are schema-checked, not
live credentials or a sequence of requests against an existing student.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
AGENT = ROOT / "agent"
OUT = BACKEND / "docs/frontend-api"
sys.path[:0] = [str(BACKEND / "src"), str(AGENT / "python")]

from walnut_backend.api.app import create_app  # noqa: E402
from walnut_backend.api.routes.feishu_mcp import TOOLS  # noqa: E402
from walnut_backend.bootstrap import ContractRelease, Settings  # noqa: E402

LABELS = {
    "getStudentBootstrap": "学生启动信息",
    "getGameBootstrap": "兼容启动信息",
    "createAgentSession": "创建会话",
    "getAgentSession": "读取会话与 Turn 序号",
    "getProductSessionWorkspace": "恢复工作区",
    "getProductContentUnit": "读取关卡内容",
    "getProductSkillDraft": "读取代码草稿",
    "upsertProductSkillDraft": "保存代码草稿",
    "createSkillBuild": "提交编译",
    "getSkillBuild": "读取编译结果",
    "activateSkillVersion": "激活技能版本",
    "getSkillActivation": "读取激活结果",
    "createAgentTurn": "运行技能／文字提问／请求代码建议",
    "getCommand": "查询异步处理状态",
    "getRun": "读取运行结果",
    "getEvidence": "读取原始证据",
    "getWorldSnapshot": "读取世界快照",
    "listWorldEvents": "读取世界事件",
    "listWorldPresentationEvents": "读取世界动画事件",
    "listProductAgentInteractions": "读取反馈／提问／总结列表",
    "getProductAgentInteraction": "读取单条反馈及建议决定",
    "recordProductPatchDecision": "接受或拒绝代码建议",
    "getInt2Capabilities": "查询可用功能",
    "ingestClientEventBatch": "批量上报客户端事件",
    "queryLearnerProjectionFromFeishu": "教师查询学生学习记录",
    "queryClassInsightsFromFeishu": "教师查询班级统计",
    "getRedactedEvidenceForFeishu": "教师读取脱敏证据",
    "feishuTeacherMcp": "教师只读 MCP 工具",
}
FLAGS = {
    "listWorldPresentationEvents": "WALNUT_ENABLE_WORLD_PRESENTATION=true",
    "recordProductPatchDecision": "WALNUT_ENABLE_SKILL_PATCH=true（同时要求 WORLD_PRESENTATION）",
    "ingestClientEventBatch": "WALNUT_ENABLE_CLIENT_EVENT_BATCH=true",
}


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def bind_refs(value, base):
    if isinstance(value, list):
        return [bind_refs(item, base) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: bind_refs(item, base) for key, item in value.items()}
    if "$ref" in result:
        filename, separator, fragment = result["$ref"].partition("#")
        target = (base.parent / filename).resolve() if filename else base
        result["$ref"] = str(target) + (separator + fragment if separator else "")
    return result


def resolve(node, base):
    while isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        filename, _, pointer = ref.partition("#")
        base = (base.parent / filename).resolve() if filename else base
        target = read(base)
        for part in pointer.lstrip("/").split("/") if pointer else []:
            target = target[part.replace("~1", "/").replace("~0", "~")]
        node = {**bind_refs(target, base), **{k: v for k, v in node.items() if k != "$ref"}}
    return node, base


def link(path):
    return os.path.relpath(path, OUT).replace("\\", "/")


def shape(node, base):
    node, base = resolve(node, base)
    props = dict(node.get("properties", {}))
    required = set(node.get("required", []))
    # allOf may add fields/constraints from a different source document.
    fields = {name: (value, base) for name, value in props.items()}
    for branch in node.get("allOf", []):
        extra, mandatory = shape(branch, base)
        for name, (value, origin) in extra.items():
            if name in fields:
                previous, previous_base = fields[name]
                previous, previous_base = resolve(previous, previous_base)
                fields[name] = (
                    {**bind_refs(previous, previous_base), **bind_refs(value, origin)},
                    origin,
                )
            else:
                fields[name] = value, origin
        required |= mandatory
    return fields, required


def describe(node, base):
    node, base = resolve(node, base)
    parts = []
    if "const" in node:
        parts.append("固定 " + json.dumps(node["const"], ensure_ascii=False))
    elif "enum" in node:
        parts.append("枚举 " + ", ".join(map(str, node["enum"])))
    elif "oneOf" in node or "anyOf" in node:
        parts.append(
            " / ".join(describe(v, base)[0] for v in node.get("oneOf", node.get("anyOf", [])))
        )
    else:
        parts.append(
            str(
                node.get(
                    "type", "object" if node.get("properties") or node.get("allOf") else "见 Schema"
                )
            )
        )
    for key in (
        "format",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "default",
    ):
        if key in node:
            parts.append(f"{key}={node[key]}")
    if node.get("additionalProperties") is False:
        parts.append("不接受额外字段")
    return "; ".join(parts), node.get("description", "")


def field_table(node, base, prefix="", depth=0):
    fields, required = shape(node, base)
    rows = []
    for name, (value, origin) in fields.items():
        typename, description = describe(value, origin)
        path = prefix + name
        rows.append((f"`{path}`", "是" if name in required else "否", f"`{typename}`", description))
        value, origin = resolve(value, origin)
        if depth < 2 and name not in {
            "request_context",
            "versions",
            "feedback_event",
            "projection_source",
        }:
            if value.get("type") == "array":
                rows += field_table(value.get("items", {}), origin, path + "[].", depth + 1)
            else:
                rows += field_table(value, origin, path + ".", depth + 1)
            for index, branch in enumerate(value.get("oneOf", []), 1):
                rows += field_table(branch, origin, f"{path}[分支{index}].", depth + 1)
    return rows


def table(rows):
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", " ")

    return [
        "| 字段 | 必填 | 类型／约束 | 说明 |",
        "| --- | --- | --- | --- |",
        *["| " + " | ".join(cell(x) for x in row) + " |" for row in rows],
    ]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "examples").mkdir(exist_ok=True)
    settings = replace(
        Settings.for_test(contract_path=AGENT),
        world_presentation_enabled=True,
        skill_patch_enabled=True,
        realtime_wss_enabled=True,
        client_event_batch_enabled=True,
    )
    validator = ContractRelease(settings)
    actual = create_app(settings).openapi()["paths"]
    specs = {}
    for source in sorted((AGENT / "contracts/openapi").glob("*.json")):
        for path, item in read(source).get("paths", {}).items():
            for method, op in item.items():
                if method in {"get", "post", "put", "patch", "delete"}:
                    specs[method, path] = op, item.get("parameters", []), source
    examples = {}
    for source in sorted((AGENT / "contracts/examples").glob("*.json")):
        sample = read(source)
        if "schema_ref" in sample and "value" in sample:
            schema = (source.parent / sample["schema_ref"]).resolve()
            if schema.exists() and not validator.validate(
                str(schema.relative_to(AGENT)).replace("\\", "/"), sample["value"]
            ):
                examples.setdefault(schema, sample["value"])
    copied = {}

    def schema_section(node, base):
        _, schema_path = resolve(node, base)
        lines = [
            f"完整字段、条件必填及嵌套约束：[Schema]({link(schema_path)})。表格中的子字段必填指父对象存在且选择该分支时。",
            "",
        ]
        rows = field_table(node, base)
        lines += table(rows) if rows else ["该结构采用组合 Schema，请查看上面的完整定义。"]
        sample = examples.get(schema_path)
        if sample is not None:
            name = schema_path.parent.name + "-" + schema_path.name.replace(".schema", "")
            (OUT / "examples" / name).write_text(
                json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            copied[name] = str(schema_path.relative_to(AGENT)).replace("\\", "/")
            lines += ["", f"完整 JSON 示例：[examples/{name}](examples/{name})。"]
        return lines

    lines = [
        "# HTTP 接口字段参考",
        "",
        "更新日期：2026-09-09。由实际挂载路由与本地合同生成；包含默认关闭但可配置启用的路由。先读 [联调主文档](README.md)。",
        "",
        "请求头和异步流程见主文档。普通接口直接返回资源 JSON，不包 `{code,data}`；MCP 是 JSON-RPC。示例是合同样例，ID、版本、哈希需替换为当前接口返回值，不是现成可用的业务数据。",
        "",
        "通用 `request_context` 与 `versions` 的完整字段见文末公共结构；字段表最多展开三层，深层结构及互斥条件以链接的 Schema 为准。",
        "",
        "| 操作 | 方法与路径 | 可用条件 |",
        "| --- | --- | --- |",
    ]
    mounted = []
    for path, item in actual.items():
        for method, op in item.items():
            operation = op["operationId"]
            assert operation in LABELS, operation
            mounted.append((method, path, operation))
            lines.append(
                f"| [{LABELS[operation]}](#{operation.lower()}) | `{method.upper()} {path}` | {FLAGS.get(operation, '路由已挂载；仍需有效身份与资源')} |"
            )
    for method, path, operation in mounted:
        lines += [
            "",
            f"## {operation}",
            "",
            f"**{LABELS[operation]}** · `{method.upper()} {path}`",
            "",
            f"启用条件：{FLAGS.get(operation, '路由始终挂载')}。",
            "",
        ]
        if operation == "feishuTeacherMcp":
            lines += ["采用 JSON-RPC 2.0；详见 [教师与可选接口](补充接口.md#mcp)。", ""]
            for tool in TOOLS:
                lines += [
                    f"### {tool['name']}",
                    "",
                    tool["description"],
                    "",
                    "```json",
                    json.dumps(tool["inputSchema"], ensure_ascii=False, indent=2),
                    "```",
                    "",
                ]
            continue
        assert (method, path) in specs, (method, path)
        op, inherited, base = specs[method, path]
        assert op["operationId"] == operation
        lines += [f"合同来源：[OpenAPI]({link(base)})。", "", "### 请求参数", ""]
        params = [resolve(p, base)[0] for p in inherited + op.get("parameters", [])]
        lines += ["| 名称 | 位置 | 必填 | 类型／约束 |", "| --- | --- | --- | --- |"]
        for param in params:
            # Resolve parameter schemas relative to the defining OpenAPI.
            original = next(
                p for p in inherited + op.get("parameters", []) if resolve(p, base)[0] == param
            )
            _, origin = resolve(original, base)
            desc = describe(param.get("schema", {}), origin)[0].replace("|", "\\|")
            lines.append(
                f"| `{param['name']}` | {param['in']} | {'是' if param.get('required') else '否'} | {desc} |"
            )
        if not params:
            lines.append("| 无额外参数 | — | — | 仍需公共请求头 |")
        if "requestBody" in op:
            body, origin = resolve(op["requestBody"], base)
            node = body["content"]["application/json"]["schema"]
            lines += ["", "### JSON 请求体", "", *schema_section(node, origin)]
        else:
            lines += ["", "请求体：无。"]
        lines += ["", "### 响应", ""]
        for code, response in op["responses"].items():
            if not code.startswith("2"):
                continue
            response, origin = resolve(response, base)
            lines += [f"**HTTP {code}**：{response.get('description', '')}", ""]
            headers = response.get("headers", {})
            if headers:
                lines += ["响应头：" + "、".join(f"`{h}`" for h in headers) + "。", ""]
            node = response.get("content", {}).get("application/json", {}).get("schema")
            if node:
                lines += schema_section(node, origin) + [""]
        if operation == "getEvidence":
            source = AGENT / "contracts/schemas/game/build-rejection-evidence.schema.json"
            lines += [
                "### 编译拒绝证据分支",
                "",
                "实际路由在 `payload.evidence_kind=BUILD_REJECTION` 时返回以下专用结构；前端按此分支解析，不能套用运行证据 payload。",
                "",
                *schema_section(read(source), source),
                "",
            ]
        errors = [code for code in op["responses"] if not code.startswith("2")]
        lines += [
            "合同声明的错误状态："
            + "、".join(errors)
            + "。具体错误码和处理方式见主文档；不要只根据 HTTP 状态推断学生代码错误。",
            "",
        ]
    lines += ["## 公共结构", ""]
    for name in ("request-context", "version-set", "error", "evidence-ref"):
        source = AGENT / f"contracts/schemas/common/{name}.schema.json"
        lines += [f"### {name}", "", *schema_section(read(source), source), ""]
    (OUT / "HTTP接口参考.md").write_text("\n".join(lines), encoding="utf-8")
    manifest = {
        "http_operations": [
            {"method": m.upper(), "path": p, "operation_id": o} for m, p, o in mounted
        ],
        "examples": copied,
        "websockets": [
            "/product-experience/v1/sessions/{session_id}/dingdang-voice",
            "/v1/realtime",
        ],
    }
    (OUT / "reference-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Generated {len(mounted)} HTTP operations and {len(copied)} schema-validated examples; no services started."
    )


if __name__ == "__main__":
    main()
