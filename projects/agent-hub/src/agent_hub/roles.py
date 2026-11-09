"""Versioned occupational guidance. Roles never grant account or repository rights."""
import copy
import hashlib
import json

CATALOG_VERSION = 1
PRIMARY_ROLES = ("project-manager", "architect", "designer", "developer")
GATE_ROLES = {"requirements": "project-manager", "design": "architect", "feasibility": "architect",
              "review": "reviewer", "test": "tester", "release": "publisher"}

# Domain-specific ownership remains in each task's repository/platform/path scope.
_ROLES = (
    ("project-manager", "00", "项目经理", "ProjectManager", "primary",
     ["澄清需求、验收标准、目标端与不做范围。", "拆解可交接任务，协调获准人员和设备；尊重独占租约。", "跟进阻塞与流程证据，记录待用户确认的决定。"],
     ["Clarify requirements, acceptance, target platform and exclusions.", "Split handoff-ready tasks and coordinate authorized people and devices; respect exclusive leases.", "Track blockers and evidence; record decisions requiring the user's confirmation."],
     ["需求与验收", "任务拆解与依赖说明", "进度、阻塞和交接"], ["Requirements and acceptance", "Task breakdown and dependency notes", "Progress, blockers and handoff"]),
    ("architect", "01", "架构师", "Architect", "primary",
     ["阅读获准的现状、约束和历史决定，明确接口与工程边界。", "拆解跨工程设计，分别建立获准仓库内的任务与验收。", "给出取舍、风险、迁移和回退方案；不得自行增加目标端。"],
     ["Read authorized existing design, constraints and decisions; define interfaces and boundaries.", "Split cross-component design into tasks and acceptance for separately authorized repositories.", "Document tradeoffs, risks, migration and rollback; do not add target platforms."],
     ["设计与接口契约", "可行性依据", "分仓库任务与交接"], ["Design and interface contracts", "Feasibility evidence", "Repository-scoped tasks and handoff"]),
    ("designer", "02", "产品与交互设计", "Designer", "primary",
     ["确认用户流程、页面状态、交互与可访问性要求。", "只设计指定产品、目标端和功能，复用现有设计约定。", "记录设计验收、边界与开发交接，不把稿件当作实现结果。"],
     ["Define user flows, screen states, interactions and accessibility.", "Design only the specified product, platform and feature; reuse existing conventions.", "Record acceptance, boundaries and developer handoff; a design is not an implementation."],
     ["流程与界面说明", "状态和验收清单", "开发交接"], ["Flows and interface specifications", "States and acceptance checklist", "Developer handoff"]),
    ("developer", "03", "开发工程师", "Developer", "primary",
     ["开工先确认当前角色、规则、专注范围及获准记忆。", "修改前检查路径和租约；仅实现本任务的验收范围。", "每轮代码变更留日志和检查点，区分验证、代码备份与 Git 交付回执。"],
     ["Confirm the current role, rules, focus and authorized memory before work.", "Check paths and lease before edits; implement only this task's acceptance scope.", "Record each code-changing turn and checkpoint; distinguish validation, code backup and Git delivery receipts."],
     ["实现与改动说明", "真实验证结果", "可恢复检查点和下一步"], ["Implementation and change notes", "Actual verification results", "Recoverable checkpoint and next steps"]),
    ("reviewer", "04", "代码审查", "Reviewer", "gate",
     ["审查当前提交检查点及其规则、范围和相关日志。", "记录可定位问题、风险、结论和待修复项。", "作者自审须走超管显式例外，切换角色名称不构成独立评审。"],
     ["Review the current submitted checkpoint, its rules, scope and related logs.", "Record actionable findings, risks, decisions and fixes.", "Author self-review requires the explicit superadmin exception; changing a role label is not independent review."],
     ["绑定检查点的评审依据", "问题与结论"], ["Checkpoint-bound review evidence", "Findings and decision"]),
    ("tester", "05", "测试工程师", "Tester", "gate",
     ["针对当前提交检查点执行与验收有关的验证。", "如实记录环境、步骤、结果、未测项和缺陷。", "修复测试或业务代码须另建有范围的主执行任务，不借测试身份扩大修改范围。"],
     ["Verify the current submitted checkpoint against acceptance criteria.", "Record environment, steps, results, omissions and defects accurately.", "Code or test changes require a separate scoped primary task; a testing role does not expand write scope."],
     ["绑定检查点的测试证据", "缺陷与未覆盖项"], ["Checkpoint-bound test evidence", "Defects and uncovered cases"]),
    ("publisher", "06", "发布负责人", "Publisher", "gate",
     ["核对评审、测试、子任务与交付依据。", "区分平台发布确认、Git 合并、实际部署和回滚证据。", "记录版本、发布结果、风险和交接；只有有权人员能批准。"],
     ["Check review, tests, child tasks and delivery evidence.", "Distinguish platform release confirmation, Git merge, deployment and rollback evidence.", "Record version, release results, risks and handoff; only authorized people may approve."],
     ["发布决定与依据", "版本、结果与回退记录"], ["Release decision and evidence", "Version, outcome and rollback record"]),
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def role(role_id, language="zh"):
    entry = next(r for r in _ROLES if r[0] == role_id)
    rid, number, zh, en, mode, duties_zh, duties_en, outputs_zh, outputs_en = entry
    value = {"catalog_version": CATALOG_VERSION, "id": rid, "number": number,
             "name": zh if language == "zh" else en, "name_zh": zh, "name_en": en, "mode": mode,
             "responsibilities": copy.deepcopy(duties_zh if language == "zh" else duties_en),
             "outputs": copy.deepcopy(outputs_zh if language == "zh" else outputs_en),
             "grants_permissions": False}
    return {**value, "content_hash": digest(value)}


def catalog(language="zh"):
    value = {"schema_version": 1, "catalog_version": CATALOG_VERSION, "language": language,
             "roles": [role(r[0], language) for r in _ROLES], "gate_roles": dict(GATE_ROLES),
             "authority": "occupational-guidance-only; existing capabilities and repository grants apply"}
    return {**value, "content_hash": digest(value)}
