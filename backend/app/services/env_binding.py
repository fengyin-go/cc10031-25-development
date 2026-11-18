"""可执行环境绑定领域服务 — 把「系统凭据」与「执行环境」在同一绑定中同时确定。

背景：
    凭据（credential_store）与执行环境（env_profile）此前分别维护，调度只检查
    “各有一条正常凭据和可用环境”，无法说明某个目标系统实际绑定了哪组凭据和
    浏览器环境。本服务读取两侧数据，解析目标系统、凭据状态与环境能力，生成
    可执行环境绑定清单，让运行批次的凭据来源和环境版本可以回查。

领域规则（处理项虎口径）：
- 凭据与环境必须在同一绑定中同时确定，绑定生成时即快照凭据名称、账户、
  浏览器版本等来源字段，运行批次不能引用被吊销凭据或已退役环境；
- 同一目标系统只允许存在一个「待生效 / 生效中」的当前可执行绑定；
- 同一目标系统同时存在即将过期凭据和刚轮换凭据时，可沿用当前绑定到过期日，
  也可立即切换到新凭据；两种处理都只能生成一个当前可执行绑定，冲突处置会
  写入绑定的 25 号处理终态（沿用至过期 / 立即轮换 / 冲突归档）；
- 重复提交只保留一条结果（按 目标系统+凭据ID+环境ID+处理口径 幂等）；
- 处理台账与结果清单联动核对；沿用既有版本口径；结果视图供概览看板展示；
- 运行时会话按绑定清单的目标系统和环境版本启自动化会话。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 凭据被吊销、环境已退役等情形下，绑定不可再被运行批次引用
BLOCKED_CREDENTIAL_STATUS = ("已过期", "已吊销")
BLOCKED_ENV_STATUS = ("维护中", "已退役")
# 允许进入当前可执行绑定的凭据/环境状态
USABLE_CREDENTIAL_STATUS = ("正常", "即将过期")
USABLE_ENV_STATUS = ("待验证", "可用")
# 仍占用“当前可执行绑定”名额的状态
CURRENT_STATUSES = ("待生效", "生效中")

# 25 号处理终态：冲突/轮换处置的最终口径，处理项虎可回查
TERMINAL_KEEP_UNTIL_EXPIRY = "沿用至过期"
TERMINAL_ROTATE_NOW = "立即轮换"
TERMINAL_CONFLICT_ARCHIVED = "冲突归档"
TERMINAL_SUPERSEDED = "已被替代"
TERMINAL_RETIRED = "失效停用"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EnvBindingService(BaseModuleService):
    """可执行环境绑定：跨 credential_store / env_profile 的组合领域服务。"""

    MODULE = "env_binding"
    ENTITY = "可执行环境绑定"
    OPERATION_TERM = "绑定"
    LIST_FIELDS = [
        "目标系统", "凭据状态", "环境版本", "绑定编号", "凭据名称", "账户标识",
        "环境名称", "操作系统", "处理口径", "生效时间", "处理终态", "来源批次",
    ]
    REQUIRED_FIELDS = ["目标系统", "凭据名称", "环境名称"]
    STATUS_ORDER = ["待生效", "生效中", "冲突", "已失效"]
    # 动作名 -> 流转目标状态（同一动作可能落在不同状态，run_action 内再细分）
    ACTION_RULES = {
        "生成绑定": "待生效",
        "沿用至过期日": "生效中",
        "立即切换新凭据": "生效中",
        "停用绑定": "已失效",
    }
    NEGATIVE_ACTIONS = ["停用绑定"]

    # ------------------------------------------------------------------
    # 源数据解析：读取系统凭据 / 执行环境
    # ------------------------------------------------------------------

    def _resolve_credential(self, name_or_id: str | int) -> dict[str, Any] | None:
        rows = store.rows("credential_store")
        for row in rows:
            if str(row.get("id")) == str(name_or_id) or row.get("凭据名称") == name_or_id:
                return row
        return None

    def _resolve_environment(self, name_or_id: str | int) -> dict[str, Any] | None:
        rows = store.rows("env_profile")
        for row in rows:
            if str(row.get("id")) == str(name_or_id) or row.get("环境名称") == name_or_id:
                return row
        return None

    def _find_current_binding(self, target_system: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if row.get("目标系统") == target_system and row.get("status") in CURRENT_STATUSES:
                return row
        return None

    # ------------------------------------------------------------------
    # 绑定生成（处理项虎主入口）
    # ------------------------------------------------------------------

    def generate_binding(
        self,
        target_system: str,
        credential_ref: str | int,
        environment_ref: str | int,
        *,
        policy: str = "生成绑定",
        source_batch: str | None = None,
        remark: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """读取凭据与环境，在同一绑定中同时确定，生成/复用一条可执行绑定。

        Returns:
            (绑定记录, 消息)；校验不通过时绑定记录为 None。
        """
        credential = self._resolve_credential(credential_ref)
        if credential is None:
            return None, f"目标系统 {target_system} 的凭据不存在：{credential_ref}"
        environment = self._resolve_environment(environment_ref)
        if environment is None:
            return None, f"执行环境不存在：{environment_ref}"

        # 运行批次不能引用被吊销凭据或已退役环境
        cred_status = str(credential.get("status", ""))
        env_status = str(environment.get("status", ""))
        if cred_status in BLOCKED_CREDENTIAL_STATUS:
            return None, f"凭据「{credential.get('凭据名称')}」状态为{cred_status}，不可建立可执行绑定"
        if env_status in BLOCKED_ENV_STATUS:
            return None, f"环境「{environment.get('环境名称')}」状态为{env_status}，不可建立可执行绑定"
        if cred_status not in USABLE_CREDENTIAL_STATUS:
            return None, f"凭据状态 {cred_status} 暂不支持绑定"
        if env_status not in USABLE_ENV_STATUS:
            return None, f"环境状态 {env_status} 暂不支持绑定"

        # 重复提交只保留一条结果：同系统 + 同凭据 + 同环境 + 同口径直接复用
        rows = store.rows(self.MODULE)
        for existing in rows:
            if (
                existing.get("目标系统") == target_system
                and existing.get("凭据ID") == credential.get("id")
                and existing.get("环境ID") == environment.get("id")
                and existing.get("处理口径") == policy
                and existing.get("status") in CURRENT_STATUSES
            ):
                return existing, "重复提交已合并，沿用既有可执行环境绑定（仅保留一条结果）"

        # 同一目标系统只能有一个当前可执行绑定：先冲突检测，再按口径处置
        current = self._find_current_binding(target_system)
        terminal = ""
        if current is not None:
            current, terminal = self._settle_conflict(current, credential, policy)
            if current is None and terminal == TERMINAL_CONFLICT_ARCHIVED:
                return None, (
                    "同目标系统已存在当前绑定且未给出轮换口径，"
                    f"冲突已归档，25号处理终态={TERMINAL_CONFLICT_ARCHIVED}"
                )

        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        # 待验证环境先落「待生效」，可用环境直接「生效中」；沿用既有版本口径
        new_status = "生效中" if env_status == "可用" else "待生效"
        binding = {
            "id": new_id,
            "status": new_status,
            "pending": new_status != "生效中",
            "abnormal": False,
            "绑定编号": f"BIND-{2000 + new_id}",
            "目标系统": target_system,
            "凭据ID": credential.get("id"),
            "凭据名称": credential.get("凭据名称"),
            "凭据状态": cred_status,
            "账户标识": credential.get("账户标识"),
            "凭据有效期": credential.get("有效期"),
            "环境ID": environment.get("id"),
            "环境名称": environment.get("环境名称"),
            "环境版本": environment.get("浏览器版本"),
            "操作系统": environment.get("操作系统"),
            "网络代理": environment.get("网络代理"),
            "处理口径": policy,
            "生效时间": _now_iso(),
            "处理终态": terminal,
            "来源批次": source_batch or "",
            "备注": remark or "",
        }
        rows.append(binding)

        store.channel.publish(
            topic="rpa.env_binding_ready",
            source=self.MODULE,
            payload={
                "binding_id": binding["绑定编号"],
                "target_system": target_system,
                "credential": binding["凭据名称"],
                "env_version": binding["环境版本"],
                "policy": policy,
                "terminal": terminal,
            },
        )
        return binding, f"可执行环境绑定已生成（{new_status}）"

    def _settle_conflict(
        self,
        current: dict[str, Any],
        new_credential: dict[str, Any],
        policy: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """冲突处置：即将过期凭据 + 刚轮换凭据并存时的 25 号处理终态。"""
        same_credential = current.get("凭据ID") == new_credential.get("id")
        if same_credential:
            # 同凭据重复建绑视为沿用，不制造第二个当前绑定
            current["处理终态"] = TERMINAL_KEEP_UNTIL_EXPIRY
            current["处理口径"] = "沿用至过期日"
            return current, TERMINAL_KEEP_UNTIL_EXPIRY
        if policy == "沿用至过期日":
            # 要求沿用的却是另一张凭据：口径冲突，归档当前绑定，不新建
            current["status"] = "冲突"
            current["pending"] = False
            current["abnormal"] = True
            current["处理终态"] = TERMINAL_CONFLICT_ARCHIVED
            return None, TERMINAL_CONFLICT_ARCHIVED
        if policy == "立即切换新凭据":
            # 立即切换到新凭据：旧绑定让位，系统内仍只剩一个当前绑定
            current["status"] = "已失效"
            current["pending"] = False
            current["处理终态"] = TERMINAL_SUPERSEDED
            return None, TERMINAL_ROTATE_NOW
        # 未明确口径的冲突：归档，不生成第二个当前绑定
        current["status"] = "冲突"
        current["pending"] = False
        current["abnormal"] = True
        current["处理终态"] = TERMINAL_CONFLICT_ARCHIVED
        return None, TERMINAL_CONFLICT_ARCHIVED

    # ------------------------------------------------------------------
    # 动作入口（资源路由 /api/env_binding/{id}/actions 统一进入）
    # ------------------------------------------------------------------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if action not in self.ACTION_RULES:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        if action == "停用绑定":
            # 停用前再核对凭据/环境是否已被吊销或退役
            blocked = self._blocked_reason(entry)
            entry["status"] = "已失效"
            entry["pending"] = False
            entry["处理终态"] = TERMINAL_RETIRED
            if blocked:
                entry["abnormal"] = True
                return entry, f"绑定已停用：{blocked}，25号处理终态={TERMINAL_RETIRED}"
            return entry, f"{self.ENTITY}已停用"

        if action == "沿用至过期日":
            if entry.get("status") not in CURRENT_STATUSES:
                return None, f"当前状态 {entry.get('status')} 不支持沿用"
            entry["处理口径"] = "沿用至过期日"
            entry["处理终态"] = TERMINAL_KEEP_UNTIL_EXPIRY
            entry["status"] = "生效中"
            entry["pending"] = False
            return entry, "已确认沿用当前绑定至凭据过期日"

        if action == "立即切换新凭据":
            target_system = str(entry.get("目标系统", ""))
            rotated = self._find_rotated_credential(target_system, int(entry.get("凭据ID", 0)))
            if rotated is None:
                return None, "未找到该目标系统刚轮换的新凭据，无法切换"
            # 旧绑定标记让位
            entry["status"] = "已失效"
            entry["pending"] = False
            entry["处理终态"] = TERMINAL_SUPERSEDED
            # 用新凭据 + 同一环境版本口径立即生成唯一当前绑定
            new_binding, message = self.generate_binding(
                target_system,
                int(rotated["id"]),
                int(entry.get("环境ID", 0)),
                policy="立即切换新凭据",
                source_batch=entry.get("来源批次") or None,
            )
            if new_binding is None:
                return None, f"切换失败：{message}"
            # 新绑定由本次显式切换产生，补记 25 号处理终态
            new_binding["处理终态"] = TERMINAL_ROTATE_NOW
            return new_binding, f"已立即切换至新凭据「{rotated.get('凭据名称')}」，25号处理终态={TERMINAL_ROTATE_NOW}"

        # 生成绑定（针对已登记记录的重试/确认场景）
        if entry.get("status") in CURRENT_STATUSES:
            return entry, "该目标系统已存在当前可执行绑定，未重复生成"
        entry["status"] = "待生效"
        entry["pending"] = True
        return entry, "绑定已重新置为待生效"

    def _find_rotated_credential(self, target_system: str, current_cred_id: int) -> dict[str, Any] | None:
        """找同一目标系统下、不同于当前凭据的可用新凭据（刚轮换）。"""
        for row in store.rows("credential_store"):
            if (
                row.get("目标系统") == target_system
                and row.get("id") != current_cred_id
                and row.get("status") in USABLE_CREDENTIAL_STATUS
            ):
                return row
        return None

    def _blocked_reason(self, binding: dict[str, Any]) -> str:
        credential = self._resolve_credential(int(binding.get("凭据ID", 0))) if binding.get("凭据ID") else None
        environment = self._resolve_environment(int(binding.get("环境ID", 0))) if binding.get("环境ID") else None
        if credential and credential.get("status") in BLOCKED_CREDENTIAL_STATUS:
            return f"凭据已{credential.get('status')}"
        if environment and environment.get("status") in BLOCKED_ENV_STATUS:
            return f"环境已{environment.get('status')}"
        return ""

    # ------------------------------------------------------------------
    # 登记入口：表单登记走幂等生成，保证重复提交只留一条
    # ------------------------------------------------------------------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [
            field for field in self.REQUIRED_FIELDS
            if not str(values.get(field) or "").strip()
        ]
        if missing:
            return None, missing
        binding, message = self.generate_binding(
            target_system=str(values["目标系统"]).strip(),
            credential_ref=str(values["凭据名称"]).strip(),
            environment_ref=str(values["环境名称"]).strip(),
            policy=str(values.get("处理口径") or "生成绑定"),
            source_batch=(str(values.get("来源批次") or "").strip() or None),
        )
        if binding is None:
            # 复用 missing 通道把业务错误返回给路由层
            return None, [message]
        return binding, []

    # ------------------------------------------------------------------
    # 运行时会话：按处理项虎口径加载绑定清单，按目标系统+环境版本启会话
    # ------------------------------------------------------------------

    def load_runtime_bindings(self, target_system: str | None = None) -> list[dict[str, Any]]:
        """运行时会话加载处理项关联的可执行环境绑定清单。

        只返回生效中且凭据未吊销、环境未退役的绑定，并再次核对源数据状态，
        确保运行批次不会引用被吊销凭据或已退役环境。
        """
        bindings = [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "生效中"
            and (target_system is None or row.get("目标系统") == target_system)
        ]
        valid: list[dict[str, Any]] = []
        for binding in bindings:
            reason = self._blocked_reason(binding)
            if reason:
                # 联动失效：源凭据/环境已不可用，绑定自动停用并记录终态
                binding["status"] = "已失效"
                binding["pending"] = False
                binding["abnormal"] = True
                binding["处理终态"] = TERMINAL_RETIRED
                store.channel.publish(
                    topic="rpa.env_binding_blocked",
                    source=self.MODULE,
                    payload={"binding_id": binding.get("绑定编号"), "reason": reason},
                )
                continue
            valid.append(binding)
        return valid

    def start_runtime_sessions(self, target_system: str | None = None) -> dict[str, Any]:
        """按目标系统和环境版本启自动化会话，并把来源回写到运行批次。

        另一个业务入口（任务队列）会跟着更新待办：每个会话生成一条排队任务，
        已存在的同绑定待办不重复创建（重复提交只保留一条结果）。
        """
        bindings = self.load_runtime_bindings(target_system)
        sessions: list[dict[str, Any]] = []
        todos_created = 0
        for binding in bindings:
            session = {
                "session_id": f"SES-{binding.get('绑定编号')}",
                "target_system": binding.get("目标系统"),
                "credential_source": binding.get("凭据名称"),
                "account": binding.get("账户标识"),
                "env_name": binding.get("环境名称"),
                "env_version": binding.get("环境版本"),
                "os": binding.get("操作系统"),
                "binding_id": binding.get("绑定编号"),
                "started_at": _now_iso(),
            }
            sessions.append(session)
            todos_created += self._enqueue_session_todo(binding, session)
            store.channel.publish(
                topic="rpa.binding_session_started",
                source=self.MODULE,
                payload=session,
            )
        return {"sessions": sessions, "started": len(sessions), "todos_created": todos_created}

    def _enqueue_session_todo(self, binding: dict[str, Any], session: dict[str, Any]) -> int:
        """会话启动联动任务队列待办；同绑定已有待办则不重复建。"""
        tasks = store.rows("task_queue")
        task_no = f"TASK-{binding.get('绑定编号')}"
        if any(task.get("任务编号") == task_no for task in tasks):
            return 0
        new_id = max((int(task.get("id", 0)) for task in tasks), default=0) + 1
        tasks.append({
            "id": new_id,
            "status": "排队中",
            "pending": True,
            "abnormal": False,
            "任务编号": task_no,
            "流程名称": f"环境绑定会话-{binding.get('目标系统')}",
            "提交时间": _now_iso()[:10],
            "优先级": "常规",
            "预计执行": "立即",
            "排队时长": "0min",
            "当前状态": "排队中",
            "绑定编号": binding.get("绑定编号"),
            "环境版本": binding.get("环境版本"),
            "凭据来源": binding.get("凭据名称"),
        })
        return 1

    # ------------------------------------------------------------------
    # 处理台账 × 结果清单联动核对 + 幂等去重
    # ------------------------------------------------------------------

    def reconcile(self) -> dict[str, Any]:
        """处理台账与结果清单联动核对：去重、失效检测、看板结果视图数据。"""
        rows = store.rows(self.MODULE)
        seen: set[tuple[Any, ...]] = set()
        duplicate_rows: list[dict[str, Any]] = []
        for row in rows:
            key = (
                row.get("目标系统"), row.get("凭据ID"),
                row.get("环境ID"), row.get("处理口径"),
            )
            if key in seen:
                duplicate_rows.append(row)
            else:
                seen.add(key)

        # 重复提交只保留一条结果：后到的重复绑定直接移除
        for duplicate in duplicate_rows:
            rows.remove(duplicate)

        current_systems = [
            row.get("目标系统") for row in rows if row.get("status") in CURRENT_STATUSES
        ]
        multi_current = sorted({
            system for system in current_systems if current_systems.count(system) > 1
        })
        blocked = [
            {"绑定编号": row.get("绑定编号"), "原因": self._blocked_reason(row)}
            for row in rows
            if row.get("status") in CURRENT_STATUSES and self._blocked_reason(row)
        ]
        return {
            "total": len(rows),
            "removed_duplicates": len(duplicate_rows),
            "current_bindings": sum(1 for row in rows if row.get("status") in CURRENT_STATUSES),
            "multi_current_systems": multi_current,
            "blocked_bindings": blocked,
            "ok": not multi_current and not blocked,
        }

    def result_view(self) -> dict[str, Any]:
        """概览看板的结果视图：按状态和 25 号处理终态聚合。"""
        rows = store.rows(self.MODULE)
        by_status = {status: 0 for status in self.STATUS_ORDER}
        by_terminal: dict[str, int] = {}
        for row in rows:
            by_status[row.get("status")] = by_status.get(row.get("status"), 0) + 1
            terminal = row.get("处理终态") or "未处置"
            by_terminal[terminal] = by_terminal.get(terminal, 0) + 1
        return {
            "total": len(rows),
            "current": sum(1 for row in rows if row.get("status") in CURRENT_STATUSES),
            "by_status": by_status,
            "by_terminal": by_terminal,
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        rows = store.rows(self.MODULE)
        return {
            **base,
            "current_bindings": sum(1 for row in rows if row.get("status") in CURRENT_STATUSES),
            "expiring_credential_bindings": sum(
                1 for row in rows if row.get("凭据状态") == "即将过期"
            ),
            "terminal_25": {
                terminal: sum(1 for row in rows if row.get("处理终态") == terminal)
                for terminal in (
                    TERMINAL_KEEP_UNTIL_EXPIRY,
                    TERMINAL_ROTATE_NOW,
                    TERMINAL_CONFLICT_ARCHIVED,
                    TERMINAL_SUPERSEDED,
                    TERMINAL_RETIRED,
                )
            },
        }


# 全局单例，供 main.py / 运行时会话复用
env_binding_service = EnvBindingService()
