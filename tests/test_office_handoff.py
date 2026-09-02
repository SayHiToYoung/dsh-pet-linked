from pet.work_state import WorkStateServer
from pet.companion_state import CompanionState


RECT = {"left": 100, "top": 100, "right": 700, "bottom": 600}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_first_office_sync_binds_visible_pet_to_root_on_desktop():
    server = WorkStateServer()
    server.set_office_rect(RECT, "root-a")
    assert server.office_root_id() == "root-a"
    assert server.desktop_list() == ["root-a"]


def test_return_to_office_survives_following_syncs():
    server = WorkStateServer()
    server.set_office_rect(RECT, "root-a")
    server.set_on_desktop("root-a", False)
    server.set_office_rect(RECT, "root-a")
    assert server.desktop_list() == []


def test_root_change_preserves_current_surface():
    server = WorkStateServer()
    server.set_office_rect(RECT, "root-a")
    server.set_office_rect(RECT, "root-b")
    assert server.desktop_list() == ["root-b"]

    server.set_on_desktop("root-b", False)
    server.set_office_rect(RECT, "root-c")
    assert server.desktop_list() == []


def test_handoff_updates_single_pet_ownership():
    server = WorkStateServer()
    server.set_office_rect(RECT, "root-a")
    # to_office：归属**不**立即清空——桌宠要先走到门口并淡出，
    # 到位后由 set_on_desktop(False) 才清空，办公区才重新渲染主控鲸（避免两处重叠）。
    server.handle_handoff({"agentId": "root-a", "dir": "to_office"})
    assert server.desktop_list() == ["root-a"]

    server.set_on_desktop("root-a", False)
    assert server.desktop_list() == []

    server.handle_handoff({"agentId": "root-a", "dir": "to_desktop"})
    assert server.desktop_list() == ["root-a"]


def test_office_state_temporarily_suppresses_legacy_beacon_callback():
    changes = []
    roots = []
    server = WorkStateServer(
        on_change=lambda *args: changes.append(args),
        on_root=lambda root: roots.append(root),
    )
    server.notify_root({"state": "thinking"})
    server.update(True, "legacy beacon")
    assert roots == [{"state": "thinking"}]
    assert changes == []

    # office 心跳失效后，相同的信标状态也必须重新接管，不能被相等判断吞掉。
    server._office_last_seen -= 8.0
    server.update(True, "legacy beacon")
    assert changes == [(True, "legacy beacon")]


def test_same_busy_cycle_detail_updates_do_not_reenter_work_animation():
    changes = []
    server = WorkStateServer(on_change=lambda *args: changes.append(args))

    assert server.update(True, "thinking-1") is True
    assert server.update(True, "thinking-2") is False
    assert server.update(True, "tool-call") is False

    assert changes == [(True, "thinking-1")]
    assert server.detail == "tool-call"


def test_pet_window_ignores_detail_only_work_state_reentry():
    from pet.window import PetWindow

    class WindowStub:
        work_state = False
        work_detail = ""
        anim = "待机"

        def __init__(self):
            self.bubbles = []
            self.animations = []

        def is_quiet(self):
            return False

        def show_bubble(self, text, duration_ms=0):
            self.bubbles.append(text)

        def _work_pool(self):
            return ["写代码"]

        def _pick(self, pool, exclude=None):
            return pool[0]

        def _switch(self, name):
            self.animations.append(name)

    win = WindowStub()
    PetWindow.set_work_state(win, True, "thinking-1")
    PetWindow.set_work_state(win, True, "thinking-2")

    assert len(win.bubbles) == 1
    assert win.animations == ["写代码"]
    assert win.work_detail == "thinking-2"


def test_office_busy_substates_only_enter_work_animation_once_per_round():
    from pet.window import PetWindow

    class LibraryStub:
        @staticmethod
        def names():
            return ["待机呼吸", "深度思考", "写代码", "轻快记录", "被吓一跳"]

    class WindowStub:
        work_state = False
        work_detail = ""
        anim = "待机呼吸"
        _mirror_state = ""
        _approval_nudging = False
        _mirror_done_shown = False
        lib = LibraryStub()

        def __init__(self):
            self.bubbles = []
            self.animations = []

        def is_quiet(self):
            return False

        def show_bubble(self, text, duration_ms=0):
            self.bubbles.append(text)

        def _work_pool(self):
            return ["写代码"]

        def _pick(self, pool, exclude=None):
            return pool[0]

        def _switch(self, name):
            self.anim = name
            self.animations.append(name)

        _first_anim_keyword = PetWindow._first_anim_keyword
        set_work_state = PetWindow.set_work_state
        mirror_agent = PetWindow.mirror_agent

    win = WindowStub()
    for state in ("thinking", "tool", "writing", "thinking", "tool"):
        win.mirror_agent({"state": state, "busy": True, "text": state})

    assert win.animations == ["深度思考"]
    assert len(win.bubbles) == 1
    assert win.work_detail == "tool"

    win.mirror_agent({"state": "error", "busy": True, "text": "error"})
    win.mirror_agent({"state": "error", "busy": True, "text": "same error"})
    assert win.animations == ["深度思考", "被吓一跳"]


def test_office_sync_returns_canonical_same_pet_state():
    server = WorkStateServer()
    response = server.sync_office({
        "connectorId": "dsh-agent-office",
        "leaseId": "page-a",
        "panelRect": RECT,
        "rootId": "root-a",
    })
    assert response["protocolVersion"] == 1
    assert response["petId"] == "shenshen"
    assert response["instanceId"]
    assert response["activeSurface"] == "desktop"
    assert response["agents"] == ["root-a"]


def test_duplicate_handoff_id_only_plays_animation_once():
    events = []
    server = WorkStateServer(on_handoff=lambda event: events.append(event))
    server.sync_office({"leaseId": "page-a", "panelRect": RECT, "rootId": "root-a"})
    to_office = server.begin_to_office("root-a", "prepare-office")
    server.set_on_desktop("root-a", False, handoff_id=to_office)
    payload = {
        "handoffId": "same-request",
        "agentId": "root-a",
        "dir": "to_desktop",
    }
    server.handle_handoff(payload)
    server.handle_handoff(payload)
    assert len(events) == 1
    assert events[0]["handoffId"] == "same-request"


def test_office_lease_expiry_recovers_visual_ownership_to_desktop():
    clock = Clock()
    companion = CompanionState(clock=clock, lease_timeout=8.0)
    recovered = []
    server = WorkStateServer(
        companion=companion,
        on_companion_recovered=lambda event: recovered.append(event),
    )
    server.sync_office({"leaseId": "page-a", "panelRect": RECT, "rootId": "root-a"})
    handoff_id = server.begin_to_office("root-a", "to-office")
    server.set_on_desktop("root-a", False, handoff_id=handoff_id)
    assert server.desktop_list() == []

    clock.now = 8.1
    event = server.expire_companion()
    assert event["reason"] == "connector_lease_expired"
    assert server.desktop_list() == ["root-a"]
    assert server.office_rect() == {}
    assert recovered and recovered[0]["recoveredToDesktop"] is True


def test_late_animation_callback_cannot_override_timeout_rollback():
    clock = Clock()
    companion = CompanionState(clock=clock, handoff_timeout=5.0)
    server = WorkStateServer(companion=companion)
    server.sync_office({"leaseId": "page-a", "panelRect": RECT, "rootId": "root-a"})
    handoff_id = server.begin_to_office("root-a", "slow-handoff")
    clock.now = 5.1
    server.expire_companion()
    assert server.desktop_list() == ["root-a"]

    # 已超时的动画晚到，不能再把桌宠藏回 Office。
    server.set_on_desktop("root-a", False, handoff_id=handoff_id)
    assert server.desktop_list() == ["root-a"]
