# -*- coding: utf-8 -*-
"""Modern context-menu layout grouped around the companion's four roles."""
from __future__ import annotations

import sys

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMenu

from .fun_entry import add_ojingjing_entry
from .quick_launch import add_quick_launch_menu
from .shared import (
    add_action,
    add_agent_link_menu,
    add_autostart,
    add_drag_physics,
    add_hide_pet,
    add_look_screen,
    add_balance,
    add_deepseek_web,
    add_harness,
    add_no_move,
    add_on_top,
    add_proactive_menu,
    add_quit,
    add_return_corner,
    add_spawn_pet,
    add_submenu,
    add_update_help,
    build_animation_categories,
    build_character_menu,
    build_size_menu,
    build_speed_menu,
    DEEPSEEK_WEB_URL,
)


def build_modern_menu(menu: QMenu, pet, template: dict) -> None:
    """Build a compact modern layout without removing any existing capability."""
    def start_group() -> None:
        actions = menu.actions()
        if actions and not actions[-1].isSeparator():
            menu.addSeparator()

    # Playful profile-style entry: deliberately exclusive to the modern menu.
    easter_egg = pet.cfg.get("menu_easter_egg", {})
    if easter_egg.get("enabled", True):
        add_ojingjing_entry(menu, easter_egg)
        menu.addSeparator()

    # 最常用的对话入口保持一级可达。
    chat = getattr(pet, "on_open_chat", None)
    if chat is not None:
        add_action(menu, "AI 对话", "chat", chat, close_on_trigger=True)

    # 鲸鱼娘：只放角色本身的外观、动作与桌面行为。
    start_group()
    whale = add_submenu(menu, "鲸鱼娘", "play")
    animations = add_submenu(whale, "播放动画", "play")
    build_animation_categories(animations, pet, icons=False, leaf_role_icons=True)
    build_character_menu(whale, pet)
    build_speed_menu(whale, pet)
    build_size_menu(whale, pet)
    add_drag_physics(whale, pet)
    add_return_corner(whale, pet)
    add_no_move(whale, pet)
    add_on_top(whale, pet)
    add_autostart(whale, pet)
    add_spawn_pet(whale, pet)

    # 陪伴：她如何感知并回应当前用户。
    companion = add_submenu(menu, "陪伴与感知", "screen")
    add_look_screen(companion, pet)
    # 主动识屏（上游移植）：仅 Windows + 有聊天能力时挂载。
    if sys.platform == "win32" and getattr(pet, "proactive_watcher", None) is not None:
        add_proactive_menu(companion, pet)

    # 连接：她进入其他 Agent 工作现场的入口。
    if getattr(pet, "agent_link_manager", None) is not None:
        connection = add_submenu(menu, "连接", "link")
        add_agent_link_menu(connection, pet)

    # 工具与实验：保留全部辅助能力，但不再抢占一级菜单。
    tools = add_submenu(menu, "工具与实验", "tools")
    add_balance(tools, pet)
    add_action(tools, "Token 花费统计", "tools", lambda: pet.show_token_cost(), close_on_trigger=True)
    add_action(tools, "Token 花费设置", "tools", lambda: pet.open_token_cost_settings(), close_on_trigger=True)
    add_harness(tools, pet)
    add_deepseek_web(tools)
    add_quick_launch_menu(tools, pet.cfg)
    add_update_help(tools, pet)

    # 高频窗口操作仍保持一级可达。
    start_group()
    add_hide_pet(menu, pet)
    modern_settings = getattr(pet, "on_open_modern_settings", None)
    if modern_settings is not None:
        add_action(menu, "桌宠设置", "settings", modern_settings, close_on_trigger=True)

    start_group()
    add_quit(menu, pet)
