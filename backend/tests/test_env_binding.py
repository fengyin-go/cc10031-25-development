"""可执行环境绑定（env_binding）领域测试。

覆盖处理项虎口径：
1. 读取凭据/环境数据生成绑定，凭据与环境在同一绑定中同时确定；
2. 被吊销凭据、已退役环境不可建立/加载绑定；
3. 同一目标系统只能有一个当前可执行绑定；
4. 即将过期凭据 + 刚轮换凭据并存：沿用至过期 / 立即切换 两种口径，
   且都只产生一个当前绑定，25 号处理终态正确；
5. 重复提交只保留一条结果（幂等）；
6. 处理台账与结果清单联动核对（reconcile 去重、冲突检测）；
7. 运行时会话按目标系统和环境版本启会话，来源可回查；
8. 通用资源路由（列表筛选/导出/动作）可用。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.main import (
    env_binding_reconcile,
    env_binding_result_view,
    env_binding_runtime,
    env_binding_start_sessions,
)
from app.routers.resource import create_entry, list_entries, run_action, export_entries
from app.schemas import ActionPayload, EntryPayload
from app.store import store
from app.services.env_binding import (
    EnvBindingService,
    TERMINAL_KEEP_UNTIL_EXPIRY,
    TERMINAL_ROTATE_NOW,
    TERMINAL_SUPERSEDED,
    TERMINAL_RETIRED,
    TERMINAL_CONFLICT_ARCHIVED,
)


class _FakeRequest:
    """资源路由只用到 request.query_params.items()，字典即可充当。

    路由函数签名里的 keyword/status 是 FastAPI Query 对象，直接调用时不会
    进入 query_params，因此桩里只放真正的筛选字段。
    """

    def __init__(self, params: dict[str, str] | None = None) -> None:
        self.query_params = params or {}


def _cred_id_by_name(name: str) -> int:
    return next(r["id"] for r in store.rows("credential_store") if r["凭据名称"] == name)


def _env_id_by_name(name: str) -> int:
    return next(r["id"] for r in store.rows("env_profile") if r["环境名称"] == name)


class TestBindingGeneration:
    """绑定生成：凭据与环境在同一绑定中同时确定。"""

    def setup_method(self) -> None:
        # 每个用例在独立表副本上操作，避免相互污染
        self._original = [dict(r) for r in store.rows("env_binding")]

    def teardown_method(self) -> None:
        store._tables["env_binding"] = [dict(r) for r in self._original]

    def test_generate_binding_snapshots_credential_and_env(self) -> None:
        svc = EnvBindingService()
        cred = _cred_id_by_name("政务数据网-申报凭据")
        env = _env_id_by_name("Chrome生产环境-124")
        binding, message = svc.generate_binding(
            "新目标系统X", cred, env, source_batch="BATCH-TEST-01")
        assert binding is not None
        assert "已生成" in message
        # 凭据来源与环境版本在同一绑定中同时确定
        assert binding["目标系统"] == "新目标系统X"
        assert binding["凭据名称"] == "政务数据网-申报凭据"
        assert binding["环境版本"] == "Chrome 124.0.6367"
        assert binding["来源批次"] == "BATCH-TEST-01"
        assert binding["status"] == "生效中"

    def test_revoked_credential_cannot_bind(self) -> None:
        svc = EnvBindingService()
        revoked = _cred_id_by_name("供应商门户-停用凭据")
        env = _env_id_by_name("Chrome生产环境-124")
        binding, message = svc.generate_binding("供应商门户", revoked, env)
        assert binding is None
        assert "已吊销" in message

    def test_retired_environment_cannot_bind(self) -> None:
        svc = EnvBindingService()
        cred = _cred_id_by_name("政务数据网-申报凭据")
        retired_env = _env_id_by_name("旧版Firefox环境-115")
        binding, message = svc.generate_binding("政务数据网", cred, retired_env)
        assert binding is None
        assert "已退役" in message


class TestSingleCurrentBinding:
    """同一目标系统只能有一个当前可执行绑定。"""

    def setup_method(self) -> None:
        self._original = [dict(r) for r in store.rows("env_binding")]

    def teardown_method(self) -> None:
        store._tables["env_binding"] = [dict(r) for r in self._original]

    def _reset_platform_a_binding(self) -> None:
        """把电商平台A的种子绑定恢复为生效中，保证用例间互不污染。"""
        for row in store.rows("env_binding"):
            if row["目标系统"] == "电商平台A":
                row["status"] = "生效中"
                row["pending"] = False
                row["abnormal"] = False
                row["处理终态"] = TERMINAL_KEEP_UNTIL_EXPIRY
                row["处理口径"] = "沿用至过期日"

    def test_only_one_current_binding_per_system(self) -> None:
        self._reset_platform_a_binding()
        svc = EnvBindingService()
        old_cred = _cred_id_by_name("电商平台A-采集账号(旧)")
        new_cred = _cred_id_by_name("电商平台A-采集账号(新)")
        env = _env_id_by_name("Chrome生产环境-124")

        # 立即切换：旧绑定让位，新绑定成为唯一当前绑定
        new_binding, _ = svc.generate_binding(
            "电商平台A", new_cred, env, policy="立即切换新凭据")
        assert new_binding is not None
        current = [
            r for r in store.rows("env_binding")
            if r["目标系统"] == "电商平台A" and r["status"] in ("待生效", "生效中")
        ]
        assert len(current) == 1
        assert current[0]["凭据ID"] == new_cred
        # 旧绑定记录 25 号处理终态
        old = next(r for r in store.rows("env_binding") if r["凭据ID"] == old_cred)
        assert old["status"] == "已失效"
        assert old["处理终态"] == TERMINAL_SUPERSEDED
        assert new_binding["处理终态"] == TERMINAL_ROTATE_NOW

    def test_keep_until_expiry_reuses_current_binding(self) -> None:
        self._reset_platform_a_binding()
        svc = EnvBindingService()
        old_cred = _cred_id_by_name("电商平台A-采集账号(旧)")
        env = _env_id_by_name("Chrome生产环境-124")

        # 沿用至过期日：用当前绑定自身凭据提交，不新建绑定
        before = len(store.rows("env_binding"))
        binding, message = svc.generate_binding(
            "电商平台A", old_cred, env, policy="沿用至过期日")
        after = len(store.rows("env_binding"))
        assert after == before
        assert binding is not None
        assert binding["凭据ID"] == old_cred  # 沿用的仍是旧凭据绑定
        assert binding["处理终态"] == TERMINAL_KEEP_UNTIL_EXPIRY
        assert "重复提交" in message or binding["处理口径"] == "沿用至过期日"
        current = [
            r for r in store.rows("env_binding")
            if r["目标系统"] == "电商平台A" and r["status"] in ("待生效", "生效中")
        ]
        assert len(current) == 1

    def test_keep_policy_with_different_credential_conflicts(self) -> None:
        self._reset_platform_a_binding()
        svc = EnvBindingService()
        new_cred = _cred_id_by_name("电商平台A-采集账号(新)")
        env = _env_id_by_name("Chrome生产环境-124")
        # 要求“沿用”却提交另一张凭据：口径冲突，归档且不新建
        before = len(store.rows("env_binding"))
        binding, message = svc.generate_binding(
            "电商平台A", new_cred, env, policy="沿用至过期日")
        after = len(store.rows("env_binding"))
        assert binding is None
        assert after == before
        assert "冲突" in message

    def test_conflict_without_policy_is_archived(self) -> None:
        self._reset_platform_a_binding()
        svc = EnvBindingService()
        new_cred = _cred_id_by_name("电商平台A-采集账号(新)")
        env = _env_id_by_name("Chrome验证环境-126")
        # 种子里电商平台A已有生效绑定，默认“生成绑定”口径遇到异凭据冲突：
        # 不生成第二个当前绑定，旧绑定进入冲突并写入25号处理终态
        before = len(store.rows("env_binding"))
        binding, message = svc.generate_binding("电商平台A", new_cred, env)
        after = len(store.rows("env_binding"))
        assert binding is None
        assert after == before
        assert "冲突" in message
        current = svc._find_current_binding("电商平台A")
        assert current is None  # 旧绑定已退出当前绑定名额
        conflicted = [
            r for r in store.rows("env_binding")
            if r["目标系统"] == "电商平台A" and r["status"] == "冲突"]
        assert len(conflicted) == 1
        assert conflicted[0]["处理终态"] == TERMINAL_CONFLICT_ARCHIVED

    def test_explicit_conflict_archive_terminal(self) -> None:
        self._reset_platform_a_binding()
        svc = EnvBindingService()
        # 直接构造一个未给出口径的冲突：调用内部处置
        current = svc._find_current_binding("电商平台A")
        new_cred = next(
            r for r in store.rows("credential_store")
            if r["凭据名称"] == "电商平台A-采集账号(新)")
        settled, terminal = svc._settle_conflict(current, new_cred, "未知口径")
        assert settled is None
        assert terminal == TERMINAL_CONFLICT_ARCHIVED
        assert current["status"] == "冲突"
        assert current["abnormal"] is True


class TestIdempotentSubmission:
    """重复提交只保留一条结果。"""

    def setup_method(self) -> None:
        self._original = [dict(r) for r in store.rows("env_binding")]

    def teardown_method(self) -> None:
        store._tables["env_binding"] = [dict(r) for r in self._original]

    def test_duplicate_submission_merged(self) -> None:
        svc = EnvBindingService()
        cred = _cred_id_by_name("政务数据网-申报凭据")
        env = _env_id_by_name("Chrome验证环境-126")
        first, _ = svc.generate_binding("幂等系统Y", cred, env)
        second, message = svc.generate_binding("幂等系统Y", cred, env)
        assert first is not None and second is not None
        assert first["id"] == second["id"]
        assert "重复提交" in message
        rows = [r for r in store.rows("env_binding") if r["目标系统"] == "幂等系统Y"]
        assert len(rows) == 1

    def test_reconcile_removes_duplicate_rows(self) -> None:
        svc = EnvBindingService()
        rows = store.rows("env_binding")
        sample = dict(rows[0])
        sample["id"] = max(int(r["id"]) for r in rows) + 1
        sample["绑定编号"] = "BIND-DUP"
        rows.append(sample)  # 人为制造一条完全重复的台账记录
        report = svc.reconcile()
        assert report["removed_duplicates"] == 1
        assert all(r.get("绑定编号") != "BIND-DUP" for r in rows)
        assert report["ok"] is True


class TestRuntimeSessions:
    """运行时按绑定清单启会话，来源与版本可回查。"""

    def setup_method(self) -> None:
        self._original = [dict(r) for r in store.rows("env_binding")]
        self._original_tasks = [dict(r) for r in store.rows("task_queue")]
        from app.services.runtime_session import runtime_session
        runtime_session._sessions.clear()

    def teardown_method(self) -> None:
        store._tables["env_binding"] = [dict(r) for r in self._original]
        store._tables["task_queue"] = [dict(r) for r in self._original_tasks]

    def test_runtime_loads_only_effective_bindings(self) -> None:
        svc = EnvBindingService()
        items = svc.load_runtime_bindings()
        # 种子中两条生效绑定；已失效（吊销凭据/退役环境）的不加载
        assert len(items) == 2
        for item in items:
            assert item["status"] == "生效中"
            assert item["凭据状态"] in ("正常", "即将过期")

    def test_runtime_filter_by_target_system(self) -> None:
        svc = EnvBindingService()
        items = svc.load_runtime_bindings("政务数据网")
        assert len(items) == 1
        assert items[0]["目标系统"] == "政务数据网"

    def test_start_sessions_carry_credential_and_version(self) -> None:
        from app.services.runtime_session import runtime_session

        plan = runtime_session.start_sessions_from_bindings("政务数据网")
        assert plan["started"] == 1
        session = plan["sessions"][0]
        assert session["target_system"] == "政务数据网"
        assert session["credential_source"] == "政务数据网-申报凭据"
        assert session["env_version"] == "Chrome 126.0.6478"
        # 会话登记到运行时会话表，可按绑定编号回查
        stored = runtime_session.get_session(session["session_id"])
        assert stored is not None
        assert stored["binding_id"] == "BIND-2002"
        # 另一个业务入口（任务队列）跟着更新待办
        todos = [
            t for t in store.rows("task_queue")
            if t.get("绑定编号") == "BIND-2002"
        ]
        assert len(todos) == 1
        assert todos[0]["status"] == "排队中"
        assert todos[0]["凭据来源"] == "政务数据网-申报凭据"
        # 重复启动只保留一条待办
        runtime_session.start_sessions_from_bindings("政务数据网")
        todos_again = [
            t for t in store.rows("task_queue")
            if t.get("绑定编号") == "BIND-2002"
        ]
        assert len(todos_again) == 1

    def test_binding_auto_retired_when_source_revoked(self) -> None:
        svc = EnvBindingService()
        # 把生效绑定的凭据改为已吊销，运行时加载应联动停用
        binding = next(
            r for r in store.rows("env_binding") if r["绑定编号"] == "BIND-2002")
        cred = next(
            r for r in store.rows("credential_store")
            if r["id"] == binding["凭据ID"])
        original_status = cred["status"]
        cred["status"] = "已吊销"
        try:
            items = svc.load_runtime_bindings("政务数据网")
            assert items == []
            assert binding["status"] == "已失效"
            assert binding["处理终态"] == TERMINAL_RETIRED
            assert binding["abnormal"] is True
        finally:
            cred["status"] = original_status


class TestResultViewAndSnapshot:
    """结果视图与领域快照（概览看板消费）。"""

    def test_result_view_aggregates_status_and_terminal(self) -> None:
        svc = EnvBindingService()
        view = svc.result_view()
        assert view["total"] == 3
        assert view["current"] == 2
        assert view["by_status"]["生效中"] == 2
        assert view["by_status"]["已失效"] == 1
        assert view["by_terminal"][TERMINAL_KEEP_UNTIL_EXPIRY] == 1
        assert view["by_terminal"][TERMINAL_RETIRED] == 1

    def test_domain_snapshot_structure(self) -> None:
        svc = EnvBindingService()
        snapshot = svc.domain_snapshot()
        assert snapshot["module"] == "env_binding"
        assert snapshot["current_bindings"] == 2
        assert snapshot["expiring_credential_bindings"] == 1
        assert "terminal_25" in snapshot


class TestResourceRouter:
    """通用资源路由：列表筛选、导出、登记、动作端到端（直接调用路由函数）。"""

    def setup_method(self) -> None:
        self._original = [dict(r) for r in store.rows("env_binding")]

    def teardown_method(self) -> None:
        store._tables["env_binding"] = [dict(r) for r in self._original]

    def test_list_with_target_system_filter(self) -> None:
        page = list_entries(
            "env_binding", _FakeRequest({"目标系统": "电商平台A"}),
            keyword=None, status=None, page=1, size=20)
        assert page.total >= 1
        assert all(item["目标系统"] == "电商平台A" for item in page.items)

    def test_list_filter_by_credential_status_and_env_version(self) -> None:
        page = list_entries(
            "env_binding",
            _FakeRequest({"凭据状态": "即将过期", "环境版本": "Chrome 124.0.6367"}),
            keyword=None, status=None, page=1, size=20,
        )
        assert page.total == 1
        assert page.items[0]["绑定编号"] == "BIND-2001"

    def test_export_csv(self) -> None:
        import asyncio

        async def _collect() -> str:
            return "".join([chunk async for chunk in response.body_iterator])

        response = export_entries(
            "env_binding", _FakeRequest({}), keyword=None, status=None)
        body = asyncio.run(_collect())
        assert "text/csv" in response.media_type
        assert "BIND-2001" in body

    def test_create_entry_is_idempotent_via_router(self) -> None:
        payload = EntryPayload(values={
            "目标系统": "路由系统Z",
            "凭据名称": "政务数据网-申报凭据",
            "环境名称": "Chrome生产环境-124",
        })
        first = create_entry("env_binding", payload)
        second = create_entry("env_binding", payload)
        assert first.ok is True and second.ok is True
        assert first.entry["id"] == second.entry["id"]

    def test_create_entry_rejects_revoked_credential(self) -> None:
        result = create_entry("env_binding", EntryPayload(values={
            "目标系统": "供应商门户",
            "凭据名称": "供应商门户-停用凭据",
            "环境名称": "Chrome生产环境-124",
        }))
        assert result.ok is False
        assert "已吊销" in result.message

    def test_action_keep_until_expiry(self) -> None:
        binding_id = next(
            r["id"] for r in store.rows("env_binding") if r["绑定编号"] == "BIND-2001")
        result = run_action("env_binding", binding_id, ActionPayload(action="沿用至过期日"))
        assert result.ok is True
        assert result.entry["处理终态"] == TERMINAL_KEEP_UNTIL_EXPIRY

    def test_action_switch_to_rotated_credential(self) -> None:
        binding_id = next(
            r["id"] for r in store.rows("env_binding") if r["绑定编号"] == "BIND-2001")
        result = run_action("env_binding", binding_id, ActionPayload(action="立即切换新凭据"))
        assert result.ok is True
        assert result.entry["凭据名称"] == "电商平台A-采集账号(新)"
        assert result.entry["处理终态"] == TERMINAL_ROTATE_NOW
        # 系统内仍只有一个当前绑定
        current = [
            r for r in store.rows("env_binding")
            if r["目标系统"] == "电商平台A" and r["status"] in ("待生效", "生效中")
        ]
        assert len(current) == 1


class TestBindingApiEndpoints:
    """main.py 中绑定专属端点（直接调用端点函数）。"""

    def setup_method(self) -> None:
        self._original = [dict(r) for r in store.rows("env_binding")]
        self._original_tasks = [dict(r) for r in store.rows("task_queue")]

    def teardown_method(self) -> None:
        store._tables["env_binding"] = [dict(r) for r in self._original]
        store._tables["task_queue"] = [dict(r) for r in self._original_tasks]

    def test_runtime_endpoint(self) -> None:
        data = env_binding_runtime(target_system=None)
        assert data["count"] == 2

    def test_start_sessions_endpoint(self) -> None:
        data = env_binding_start_sessions(target_system="政务数据网")
        assert data["ok"] is True
        assert data["started"] == 1

    def test_reconcile_endpoint(self) -> None:
        data = env_binding_reconcile()
        assert "removed_duplicates" in data

    def test_result_view_endpoint(self) -> None:
        assert env_binding_result_view()["current"] == 2
