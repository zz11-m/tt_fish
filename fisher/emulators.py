# SPDX-License-Identifier: GPL-3.0-only
"""模拟器窗口筛选与选区归属；不保存窗口句柄或其他应用资料。"""

EMULATORS = {"ldplayer": "雷电模拟器", "mumu": "MuMu模拟器"}
ALIASES = {"ldplayer": ("雷电", "ldplayer"), "mumu": ("mumu",)}
MANAGERS = ("多开器", "多开管理", "多开助手", "multi-instance", "multi instance", "multiplayer", "manager")


def title_family(title):
    title = str(title).casefold()
    for family, aliases in ALIASES.items():
        if any(alias in title for alias in aliases):
            return family
    return None


def select_windows(windows, emulator_type, include_other=False):
    if emulator_type not in EMULATORS:
        raise ValueError("请选择雷电模拟器或MuMu模拟器")
    candidates = []
    for hwnd, title in windows:
        lower = title.casefold()
        if "tt_fish" in lower or any(part in lower for part in MANAGERS):
            continue
        family = title_family(title)
        if family == emulator_type or include_other:
            candidates.append((hwnd, title))
    return sorted(candidates, key=lambda row: (title_family(row[1]) != emulator_type, row[1].casefold(), row[0]))


def profile_matches(profile, emulator_type, title, client_size):
    if not isinstance(profile, dict):
        return False
    # 旧版未保存模拟器种类；只对可从标题确认归属的窗口复用选区。
    family = profile.get("emulator_type") or title_family(profile.get("title", ""))
    return (family == emulator_type and profile.get("title") == title and
            profile.get("client_size") == list(client_size) and
            isinstance(profile.get("crop"), list) and len(profile["crop"]) == 4)
