from pet.work_state import WorkStateServer


RECT = {"left": 100, "top": 100, "right": 700, "bottom": 600}


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
