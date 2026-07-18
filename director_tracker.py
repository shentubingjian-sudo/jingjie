#!/usr/bin/env python3
"""
镜界 v2.6 导演状态与剧情卡运行时
=================================

职责：
- 维护导演数值、滑动窗口、冷却、暂停状态和契诃夫道具
- 以可复现随机方式选择导演动作
- 维护剧情卡 cooldown / max_triggers / sticky / delay / chain
- 提供原子写入、revision 并发检查、idempotency 去重
- 创建、预览和回滚完整状态快照

边界：
- 本脚本不生成叙事文本
- 本脚本不做向量嵌入；语义分数由宿主传入
- 本脚本不保存模型隐藏思维，只接收明确的结构化状态补丁
- 优先接收 JSON；兼容旧版 @@s 简单 YAML

仅使用 Python 标准库。
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import copy
import hashlib
import json
import os
import random
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

try:  # Unix/macOS 文件锁
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # P0-2.5: Windows 文件锁
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - Unix/macOS
    msvcrt = None


RUNTIME_VERSION = "2.7.2"
SCHEMA_VERSION = "2.6"
WINDOW_SIZE = 8
GENERAL_COOLDOWN = 2
CONSECUTIVE_MAX = 3
CONSECUTIVE_SILENCE = 3
DAILY_COOLDOWN = 5
RECYCLE_COOLDOWN = 3
CHECKPOINT_LIMIT_PER_KIND = 10
EVENT_LOG_LIMIT = 200
IDEMPOTENCY_LIMIT = 100
CHAIN_DEPTH_LIMIT = 2
CHAIN_QUEUE_TTL = 2

VALID_ACTIONS = {"升压", "降压", "给突破口", "加阻力", "推动回收", "日常过渡"}
VALID_PAUSE_LEVELS = {"L1", "L2", "L3", "L4"}
VALID_PAUSE_STATUSES = {"RUNNING", "PAUSED"}
# P1-3.1: 合法的暂停层级+状态组合（L1/L2/L4 必须 PAUSED，L3 必须 RUNNING）
VALID_PAUSE_COMBINATIONS = {
    ("L1", "PAUSED"),
    ("L2", "PAUSED"),
    ("L3", "RUNNING"),
    ("L4", "PAUSED"),
}
VALID_CARD_POSITIONS = {"core_rule", "world_context", "recent_focus"}
VALID_DELAY_MODES = {"continuous", "latched"}
VALID_CARD_SOURCES = {"author", "auto_generated", "imported"}
VALID_CARD_TYPES = {"chekhov_gun", "encounter", "revelation", "crisis", "character_moment", "world_event"}

ACTION_WEIGHTS: dict[str, list[tuple[Optional[str], float]]] = {
    "沉浸中": [(None, 1.0)],
    "趋近无聊": [("升压", 0.70), ("加阻力", 0.30)],
    "趋近焦虑": [("降压", 0.60), ("给突破口", 0.40)],
    "失去方向": [("给突破口", 0.50), ("推动回收", 0.50)],
    "高能后回落": [("日常过渡", 0.90), (None, 0.10)],
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_state_file() -> Path:
    env_path = os.environ.get("JINGJIE_STATE_FILE")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return Path(__file__).with_name("director_state.json")


def _new_branch_id(prefix: str = "branch") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def init_state(session_id: Optional[str] = None, seed: Optional[str] = None) -> dict[str, Any]:
    session_id = session_id or uuid.uuid4().hex[:12]
    seed = seed or uuid.uuid4().hex
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "session_id": session_id,
        "seed": seed,
        "started_at": now_iso(),
        "updated_at": now_iso(),
        "total_rounds": 0,
        "ats_window": [],
        "flow_state": "沉浸中",
        "flow_rounds_in_state": 0,
        "pause": {
            "level": "L3",
            "status": "RUNNING",
            "auto_advance_count": 0,
            "cooldown_threshold": 3,
        },
        "director": {
            "last_action": None,
            "last_action_round": None,
            "action_cooldown_remaining": 0,
            "consecutive_interventions": 0,
            "rounds_since_intervention": 0,
            "consecutive_silence_required": 0,
            "daily_transition_cooldown": 0,
            "push_recycle_cooldown": 0,
            "intervention_history": [],
        },
        "chekhov_items": {},
        "cards": {},
        "chain_queue": [],
        "npc_attitude_log": [],
        "timeline": {
            "current_branch_id": "branch_main",
            "parent_branch_id": None,
            "abandoned_branches": [],
            "metaknowledge_holders": [],
        },
        "checkpoints": [],
        "event_log": [],
        "idempotency_keys": [],
    }


def migrate_state(raw: dict[str, Any]) -> dict[str, Any]:
    """将 v2.0/v2.5 状态迁移到 v2.6，尽量保留已有数据。"""
    if raw.get("schema_version") == SCHEMA_VERSION:
        state = raw
    else:
        state = init_state(raw.get("session_id"), raw.get("seed"))
        state["started_at"] = raw.get("started_at", state["started_at"])
        state["total_rounds"] = int(raw.get("total_rounds", 0))
        state["ats_window"] = list(raw.get("ats_window", []))[-WINDOW_SIZE:]
        state["flow_state"] = raw.get("flow_state", "沉浸中")
        state["flow_rounds_in_state"] = int(raw.get("flow_rounds_in_state", 0))
        state["director"].update(raw.get("director", {}))
        state["npc_attitude_log"] = list(raw.get("npc_attitude_log", []))

        old_items = raw.get("chekhov_items", {})
        if isinstance(old_items, list):
            for item in old_items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("item") or "").strip()
                if name:
                    state["chekhov_items"][name] = item
        elif isinstance(old_items, dict):
            state["chekhov_items"] = old_items

        old_cards = raw.get("cards", {})
        if isinstance(old_cards, dict):
            for card_id, runtime in old_cards.items():
                spec = {"id": card_id}
                if isinstance(runtime, dict):
                    spec.update(runtime)
                # 迁移模式：宽松校验，非法枚举用默认值
                state["cards"][card_id] = normalize_card_spec(spec, strict=False)

        for key in ("pause", "timeline"):
            if isinstance(raw.get(key), dict):
                state[key].update(raw[key])
        state["checkpoints"] = list(raw.get("checkpoints", []))
        state["event_log"] = list(raw.get("event_log", []))[-EVENT_LOG_LIMIT:]
        state["idempotency_keys"] = list(raw.get("idempotency_keys", []))[-IDEMPOTENCY_LIMIT:]

    # 补齐未来版本新增键，避免半迁移状态崩溃
    defaults = init_state(state.get("session_id"), state.get("seed"))
    for key, value in defaults.items():
        state.setdefault(key, copy.deepcopy(value))
    for nested in ("pause", "director", "timeline"):
        for key, value in defaults[nested].items():
            state[nested].setdefault(key, copy.deepcopy(value))
    state["schema_version"] = SCHEMA_VERSION
    state["revision"] = int(state.get("revision", 0))
    state["ats_window"] = list(state.get("ats_window", []))[-WINDOW_SIZE:]
    state["event_log"] = list(state.get("event_log", []))[-EVENT_LOG_LIMIT:]
    state["idempotency_keys"] = list(state.get("idempotency_keys", []))[-IDEMPOTENCY_LIMIT:]
    return state


@contextlib.contextmanager
def state_lock(state_file: Path) -> Iterator[None]:
    """P0-2.5: 跨平台文件锁 — Unix 用 fcntl.flock，Windows 用 msvcrt.locking"""
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    with open(lock_path, "a+b") as lock_fp:
        if fcntl is not None:
            fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:
            # Windows: 写入一个字节并锁定它
            lock_fp.seek(0)
            lock_fp.write(b"\0")
            lock_fp.flush()
            lock_fp.seek(0)
            msvcrt.locking(lock_fp.fileno(), msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                lock_fp.seek(0)
                try:
                    msvcrt.locking(lock_fp.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass  # 锁可能已释放


def load_state(state_file: Path, required: bool = False) -> Optional[dict[str, Any]]:
    if not state_file.exists():
        if required:
            raise FileNotFoundError(f"状态文件不存在：{state_file}")
        return None
    try:
        with open(state_file, "r", encoding="utf-8") as fp:
            raw = json.load(fp)
    except json.JSONDecodeError as exc:
        raise ValueError(f"状态文件 JSON 损坏：{exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("状态文件顶层必须是 JSON object")
    return migrate_state(raw)


def atomic_save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = now_iso()
    fd, temp_name = tempfile.mkstemp(
        prefix=state_file.name + ".",
        suffix=".tmp",
        dir=str(state_file.parent),
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(temp_name, state_file)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def append_event(state: dict[str, Any], event_type: str, payload: Optional[dict[str, Any]] = None) -> None:
    state["event_log"].append(
        {
            "at": now_iso(),
            "round": state["total_rounds"],
            "revision": state["revision"],
            "type": event_type,
            "payload": payload or {},
        }
    )
    state["event_log"] = state["event_log"][-EVENT_LOG_LIMIT:]


def commit_state(state_file: Path, state: dict[str, Any], event_type: str, payload: Optional[dict[str, Any]] = None) -> None:
    state["revision"] = int(state.get("revision", 0)) + 1
    append_event(state, event_type, payload)
    atomic_save_state(state_file, state)


def parse_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return None
    lowered = value.lower()
    if lowered in {"null", "none", "~"}:
        return None
    if lowered in {"true", "yes"}:
        return True
    if lowered in {"false", "no"}:
        return False
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        pass
    try:
        return ast.literal_eval(value)
    except (ValueError, SyntaxError):
        pass
    return value.strip('"\'')


def parse_legacy_yaml(text: str) -> dict[str, Any]:
    """兼容旧版简单 YAML。复杂嵌套请改用 JSON。"""
    result: dict[str, Any] = {}
    current_key: Optional[str] = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        stripped = raw_line.strip()
        if stripped == "@@s":
            continue
        if stripped.startswith("- ") and current_key:
            result.setdefault(current_key, [])
            if isinstance(result[current_key], list):
                result[current_key].append(parse_scalar(stripped[2:]))
            continue
        if ":" in stripped:
            key, value = stripped.split(":", 1)
            key = key.strip()
            current_key = key
            parsed = parse_scalar(value)
            result[key] = [] if parsed is None else parsed
    return result


def parse_patch(text: str) -> dict[str, Any]:
    text = text.strip()
    if not text:
        raise ValueError("未收到状态补丁")
    if text.startswith("@@s"):
        text = text[3:].lstrip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # type: ignore
        except ImportError:
            data = parse_legacy_yaml(text)
        else:
            data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("状态补丁顶层必须是 object")
    if isinstance(data.get("state_patch"), dict):
        data = data["state_patch"]
    return data


def clamp_number(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def normalize_metrics(patch: dict[str, Any]) -> dict[str, Any]:
    return {
        "scene_intensity": clamp_number(patch.get("scene_intensity"), 1, 10, 5),
        "chaos_proximity": clamp_number(patch.get("chaos_proximity"), 1, 10, 5),
        "player_agency": clamp_number(patch.get("player_agency"), 1, 10, 5),
        "goal_progress": clamp_number(patch.get("goal_progress"), 0, 100, 50),
        "active_npcs": patch.get("active_npcs", {}),
        "npc_attitude_shifts": patch.get("npc_attitude_shifts", []),
        "pending_flags": patch.get("pending_flags", []),
        "npc_reflections": patch.get("npc_reflections", []),
        "branch_id": patch.get("branch_id"),
    }


def analyze_trends(window: list[dict[str, Any]]) -> dict[str, str]:
    labels = {
        "scene_intensity": "scene_intensity_trend",
        "chaos_proximity": "chaos_proximity_trend",
        "player_agency": "player_agency_trend",
        "goal_progress": "goal_progress_trend",
    }
    if len(window) < 4:
        return {label: "→" for label in labels.values()}

    if len(window) >= 6:
        early, recent = window[-6:-3], window[-3:]
    else:
        mid = len(window) // 2
        early, recent = window[:mid], window[mid:]

    trends: dict[str, str] = {}
    for key, label in labels.items():
        early_avg = sum(float(row.get(key, 5)) for row in early) / len(early)
        recent_avg = sum(float(row.get(key, 5)) for row in recent) / len(recent)
        diff = recent_avg - early_avg
        trends[label] = "↑" if diff > 1.0 else "↓" if diff < -1.0 else "→"
    return trends


def determine_flow_state(trends: dict[str, str], window: list[dict[str, Any]]) -> str:
    if len(window) < 4:
        return "沉浸中"
    latest = window[-1]
    si = int(latest.get("scene_intensity", 5))
    cp = int(latest.get("chaos_proximity", 5))
    pa = int(latest.get("player_agency", 5))
    si_t = trends.get("scene_intensity_trend", "→")
    cp_t = trends.get("chaos_proximity_trend", "→")
    pa_t = trends.get("player_agency_trend", "→")
    gp_t = trends.get("goal_progress_trend", "→")

    if cp_t == "↑" and pa_t == "↓" and cp >= 7:
        return "趋近焦虑"

    recent4 = window[-4:]
    gp_values4 = [int(row.get("goal_progress", 50)) for row in recent4]
    if si_t == "↓" and gp_t == "→" and si <= 4 and max(gp_values4) - min(gp_values4) <= 5:
        return "趋近无聊"

    if len(window) >= 6:
        recent6 = window[-6:]
        gp_values6 = [int(row.get("goal_progress", 50)) for row in recent6]
        pa_values6 = [int(row.get("player_agency", 5)) for row in recent6]
        if max(gp_values6) - min(gp_values6) <= 5 and max(pa_values6) - min(pa_values6) <= 2:
            return "失去方向"

    if si_t == "↓" and pa_t == "↑":
        prior = window[-6:-3] if len(window) >= 6 else window[:-2]
        if prior and sum(int(row.get("scene_intensity", 5)) for row in prior) / len(prior) >= 7:
            return "高能后回落"

    return "沉浸中"


def tick_cooldowns(state: dict[str, Any]) -> None:
    director = state["director"]
    for key in (
        "action_cooldown_remaining",
        "consecutive_silence_required",
        "daily_transition_cooldown",
        "push_recycle_cooldown",
    ):
        director[key] = max(0, int(director.get(key, 0)) - 1)


def apply_director_action(state: dict[str, Any], action: Optional[str]) -> None:
    director = state["director"]
    if not action or action in {"none", "静默", "不干预"}:
        director["rounds_since_intervention"] = int(director.get("rounds_since_intervention", 0)) + 1
        if director["rounds_since_intervention"] >= 3:
            director["consecutive_interventions"] = 0
        return
    if action not in VALID_ACTIONS:
        raise ValueError(f"未知导演动作：{action}")

    director["last_action"] = action
    director["last_action_round"] = state["total_rounds"]
    director["rounds_since_intervention"] = 0
    director["action_cooldown_remaining"] = GENERAL_COOLDOWN
    director["consecutive_interventions"] = int(director.get("consecutive_interventions", 0)) + 1
    director["intervention_history"].append({"round": state["total_rounds"], "action": action})
    director["intervention_history"] = director["intervention_history"][-8:]

    if action == "日常过渡":
        director["daily_transition_cooldown"] = DAILY_COOLDOWN
    elif action == "推动回收":
        director["push_recycle_cooldown"] = RECYCLE_COOLDOWN

    if director["consecutive_interventions"] >= CONSECUTIVE_MAX:
        director["consecutive_silence_required"] = CONSECUTIVE_SILENCE
        director["consecutive_interventions"] = 0


def can_intervene(state: dict[str, Any], action: Optional[str] = None) -> tuple[bool, str]:
    pause = state["pause"]
    if pause.get("status") == "PAUSED" or pause.get("level") in {"L1", "L2", "L4"}:
        return False, f"暂停状态 {pause.get('level')}"

    director = state["director"]
    if int(director.get("consecutive_silence_required", 0)) > 0:
        return False, f"强制静默 {director['consecutive_silence_required']} 轮"
    if int(director.get("action_cooldown_remaining", 0)) > 0:
        return False, f"通用冷却 {director['action_cooldown_remaining']} 轮"
    if action == "日常过渡" and int(director.get("daily_transition_cooldown", 0)) > 0:
        return False, f"日常过渡冷却 {director['daily_transition_cooldown']} 轮"
    if action == "推动回收" and int(director.get("push_recycle_cooldown", 0)) > 0:
        return False, f"推动回收冷却 {director['push_recycle_cooldown']} 轮"

    last_action = director.get("last_action")
    last_action_round = director.get("last_action_round")
    recent_opposite = (
        last_action_round is not None
        and state["total_rounds"] - int(last_action_round) <= GENERAL_COOLDOWN + 1
    )
    if recent_opposite and (last_action, action) in {("升压", "降压"), ("降压", "升压")}:
        return False, "防抽搐：最近一次干预方向相反"
    return True, "可以干预"


def deterministic_choice(state: dict[str, Any], choices: list[tuple[Optional[str], float]]) -> Optional[str]:
    material = f"{state['seed']}|{state['total_rounds']}|{state['revision']}|{state['flow_state']}"
    seed_int = int(hashlib.sha256(material.encode("utf-8")).hexdigest()[:16], 16)
    rng = random.Random(seed_int)
    total = sum(max(0.0, weight) for _, weight in choices)
    if total <= 0:
        return None
    target = rng.random() * total
    cumulative = 0.0
    for action, weight in choices:
        cumulative += max(0.0, weight)
        if target <= cumulative:
            return action
    return choices[-1][0]


def choose_director_action(state: dict[str, Any]) -> dict[str, Any]:
    can, reason = can_intervene(state)
    if not can:
        return {"action": None, "reason": reason, "flow_state": state["flow_state"], "deterministic": True}

    choices = list(ACTION_WEIGHTS.get(state["flow_state"], [(None, 1.0)]))
    high_urgency = [
        item for item in state["chekhov_items"].values()
        if item.get("status") == "unresolved" and item.get("urgency") == "高"
    ]
    if high_urgency:
        choices.append(("推动回收", 0.40))

    # 先过滤动作专属冷却；None 永远可用
    filtered: list[tuple[Optional[str], float]] = []
    rejected: dict[str, str] = {}
    for action, weight in choices:
        if action is None:
            filtered.append((action, weight))
            continue
        allowed, action_reason = can_intervene(state, action)
        if allowed:
            filtered.append((action, weight))
        else:
            rejected[action] = action_reason

    action = deterministic_choice(state, filtered) if filtered else None
    return {
        "action": action,
        "reason": f"心流={state['flow_state']}；按 session seed 可复现抽取",
        "flow_state": state["flow_state"],
        "deterministic": True,
        "high_urgency_chekhov": [item["name"] for item in high_urgency],
        "rejected_actions": rejected,
    }


def urgency_from_rounds(rounds_unused: int) -> str:
    if rounds_unused >= 10:
        return "高"
    if rounds_unused >= 5:
        return "中"
    return "低"


def tick_chekhov_items(state: dict[str, Any]) -> None:
    for item in state["chekhov_items"].values():
        if item.get("status") == "unresolved":
            item["rounds_unused"] = int(item.get("rounds_unused", 0)) + 1
            item["urgency"] = urgency_from_rounds(item["rounds_unused"])


def add_or_refresh_chekhov(state: dict[str, Any], item_data: Any) -> None:
    if isinstance(item_data, str):
        item_data = {"name": item_data}
    if not isinstance(item_data, dict):
        return
    name = str(item_data.get("name") or item_data.get("item") or "").strip()
    if not name:
        return
    item = state["chekhov_items"].setdefault(
        name,
        {
            "name": name,
            "acquired_round": state["total_rounds"],
            "rounds_unused": 0,
            "urgency": "低",
            "status": "unresolved",
            "branch_id": state["timeline"]["current_branch_id"],
        },
    )
    item["status"] = "unresolved"
    item["last_seen_round"] = state["total_rounds"]
    for key in ("description", "related_characters", "scene_id"):
        if key in item_data:
            item[key] = item_data[key]


def resolve_chekhov(state: dict[str, Any], name: str) -> None:
    item = state["chekhov_items"].get(name)
    if item:
        item["status"] = "resolved"
        item["resolved_round"] = state["total_rounds"]


def _require_enum(field: str, value: Any, allowed: set[str], default: str, strict: bool = True) -> str:
    """P1-3.4: 枚举校验 — strict=True 时非法值报错，strict=False 时用默认值（用于迁移）"""
    if value is None:
        return default
    if value not in allowed:
        if strict:
            raise ValueError(f"{field} 非法：{value!r}；允许值：{sorted(allowed)}")
        return default
    return str(value)


def normalize_card_spec(spec: dict[str, Any], strict: bool = True) -> dict[str, Any]:
    """卡片规范化。strict=True（新注册）时非法枚举报错；strict=False（迁移）时用默认值"""
    card_id = str(spec.get("id") or spec.get("card_id") or "").strip()
    if not card_id:
        raise ValueError("剧情卡缺少 id")
    priority = _require_enum("priority", spec.get("priority"), {"高", "中", "低"}, "中", strict)
    position = _require_enum("inject_position", spec.get("inject_position"), VALID_CARD_POSITIONS, "recent_focus", strict)
    delay_mode = _require_enum("delay_mode", spec.get("delay_mode"), VALID_DELAY_MODES, "latched", strict)
    card_type = _require_enum("type", spec.get("type"), VALID_CARD_TYPES, "world_event", strict)
    # P1-3.2: 保留 source 字段
    source = _require_enum("source", spec.get("source"), VALID_CARD_SOURCES, "author", strict)

    return {
        "id": card_id,
        "type": card_type,
        "source": source,  # P1-3.2: 保留来源标记
        "trigger_hint": spec.get("trigger_hint", ""),
        "director_action": spec.get("director_action", "任意"),
        "priority": priority,
        "cooldown_rounds": max(0, int(spec.get("cooldown_rounds", 0))),
        "max_triggers": max(0, int(spec.get("max_triggers", 0))),  # 0 = 无限
        "content": spec.get("content", ""),
        "required_conditions": list(spec.get("required_conditions", [])),
        "required_absent": list(spec.get("required_absent", [])),
        "sticky_rounds": max(0, int(spec.get("sticky_rounds", 0))),
        "delay_rounds": max(0, int(spec.get("delay_rounds", 0))),
        "delay_mode": delay_mode,
        "chain_triggers": list(spec.get("chain_triggers", [])),
        "inject_position": position,
        "inject_order": int(spec.get("inject_order", 300 if position == "recent_focus" else 100)),
        "times_triggered": int(spec.get("times_triggered", 0)),
        "last_triggered_round": spec.get("last_triggered_round"),
        "cooldown_until_round": spec.get("cooldown_until_round"),
        "condition_first_met_round": spec.get("condition_first_met_round"),
        "conditions_met": bool(
            spec.get(
                "conditions_met",
                not spec.get("required_conditions") and not spec.get("required_absent"),
            )
        ),
        "sticky_until_round": spec.get("sticky_until_round"),
        "last_chain_depth": int(spec.get("last_chain_depth", 0)),
        "enabled": bool(spec.get("enabled", True)),
    }


def register_cards(state: dict[str, Any], specs: Iterable[dict[str, Any]]) -> list[str]:
    registered: list[str] = []
    for raw_spec in specs:
        spec = normalize_card_spec(raw_spec)
        existing = state["cards"].get(spec["id"], {})
        # 保留运行时统计，除非调用方明确提供
        for key in (
            "times_triggered",
            "last_triggered_round",
            "cooldown_until_round",
            "condition_first_met_round",
            "conditions_met",
            "sticky_until_round",
            "last_chain_depth",
        ):
            if key not in raw_spec and key in existing:
                spec[key] = existing[key]
        if spec["conditions_met"] and spec.get("condition_first_met_round") is None:
            spec["condition_first_met_round"] = state["total_rounds"]
        state["cards"][spec["id"]] = spec
        registered.append(spec["id"])
    return registered


def _require_json_bool(value: Any, field_name: str) -> bool:
    """P1-3.5: 严格校验 JSON boolean，拒绝字符串 "false"/"true" 等"""
    if not isinstance(value, bool):
        raise ValueError(
            f"{field_name} 必须为 JSON boolean (true/false)，收到：{value!r} ({type(value).__name__})"
        )
    return value


def update_card_condition(state: dict[str, Any], card_id: str, met: bool) -> None:
    card = state["cards"].get(card_id)
    if not card:
        raise KeyError(f"剧情卡未注册：{card_id}")

    if met:
        # 条件满足：记录首次满足轮次，标记条件已满足
        card["conditions_met"] = True
        if card.get("condition_first_met_round") is None:
            card["condition_first_met_round"] = state["total_rounds"]
        return

    # 条件不满足
    if card.get("delay_mode") == "continuous":
        # P0-2.1: continuous 模式 — 条件失效后清零
        card["conditions_met"] = False
        card["condition_first_met_round"] = None
    else:
        # P0-2.1: latched 模式 — 一旦曾经满足，继续保持锁定
        card["conditions_met"] = card.get("condition_first_met_round") is not None


def card_chain_boost(state: dict[str, Any], card_id: str) -> tuple[float, int]:
    current_round = state["total_rounds"]
    best_depth = 0
    for entry in state["chain_queue"]:
        if entry.get("card_id") == card_id and int(entry.get("activate_round", 0)) <= current_round <= int(entry.get("expires_round", 0)):
            best_depth = max(best_depth, int(entry.get("depth", 1)))
    return (1.5 if best_depth else 1.0), best_depth


def can_trigger_card(state: dict[str, Any], card_id: str) -> tuple[bool, str]:
    card = state["cards"].get(card_id)
    if not card:
        return False, "剧情卡未注册"
    if not card.get("enabled", True):
        return False, "剧情卡已禁用"
    if card.get("sticky_until_round") is not None and state["total_rounds"] <= int(card["sticky_until_round"]):
        return False, "sticky 余波中"
    max_triggers = int(card.get("max_triggers", 0))
    if max_triggers > 0 and int(card.get("times_triggered", 0)) >= max_triggers:
        return False, f"已达最大触发次数 {max_triggers}"
    cooldown_until = card.get("cooldown_until_round")
    if cooldown_until is not None and state["total_rounds"] <= int(cooldown_until):
        remaining = int(cooldown_until) - state["total_rounds"] + 1
        return False, f"冷却中，还需 {remaining} 轮"
    if not card.get("conditions_met", False):
        return False, "触发条件未满足"
    first_met = card.get("condition_first_met_round")
    if first_met is None:
        return False, "尚未记录条件首次满足轮次"
    ready_round = int(first_met) + int(card.get("delay_rounds", 0))
    if state["total_rounds"] < ready_round:
        return False, f"delay 等待中，第 {ready_round} 轮可触发"
    return True, "可以触发"


def get_card_freshness(state: dict[str, Any], card_id: str) -> float:
    card = state["cards"].get(card_id)
    if not card or int(card.get("times_triggered", 0)) == 0:
        return 1.0
    cooldown = int(card.get("cooldown_rounds", 0))
    if cooldown <= 0:
        return 1.0
    last = int(card.get("last_triggered_round") or 0)
    rounds_since = max(0, state["total_rounds"] - last)
    return min(1.0, 0.7 + min(1.0, rounds_since / cooldown) * 0.3)


def action_match_score(card_action: str, director_action: Optional[str]) -> float:
    if card_action in {None, "任意", "any"}:
        return 0.5
    if director_action and card_action == director_action:
        return 1.0
    return 0.0


def priority_score(priority: str) -> float:
    return {"高": 1.0, "中": 0.6, "低": 0.3}.get(priority, 0.6)


def select_card(state: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    director_action = payload.get("director_action")
    candidates = payload.get("candidates", [])
    allow_semantic_breakthrough = bool(payload.get("allow_semantic_breakthrough", True))
    # P0-2.3: 支持从 payload 临时传入卡片条件（只读选卡，不写状态）
    temp_conditions = payload.get("conditions", {})
    scored: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        card_id = str(candidate.get("card_id") or candidate.get("id") or "")
        card = state["cards"].get(card_id)
        if not card:
            excluded.append({"card_id": card_id, "reason": "未注册"})
            continue

        # P0-2.3: 如果 payload 带了临时条件，先用它更新内存中的卡片状态（不落盘）
        if card_id in temp_conditions:
            temp_met = _require_json_bool(temp_conditions[card_id], f"conditions.{card_id}")
            # 临时更新条件，模拟 update_card_condition 的效果
            if temp_met:
                card["conditions_met"] = True
                if card.get("condition_first_met_round") is None:
                    card["condition_first_met_round"] = state["total_rounds"]
            elif card.get("delay_mode") == "continuous":
                card["conditions_met"] = False
                card["condition_first_met_round"] = None
            else:
                card["conditions_met"] = card.get("condition_first_met_round") is not None

        can, reason = can_trigger_card(state, card_id)
        if not can:
            excluded.append({"card_id": card_id, "reason": reason})
            continue

        semantic = max(0.0, min(1.0, float(candidate.get("semantic", 0.0))))
        chain_multiplier, chain_depth = card_chain_boost(state, card_id)
        semantic_effective = min(1.0, semantic * chain_multiplier)
        action_score = action_match_score(str(card.get("director_action", "任意")), director_action)
        fresh = get_card_freshness(state, card_id)
        total = (
            semantic_effective * 0.40
            + action_score * 0.30
            + priority_score(str(card.get("priority", "中"))) * 0.20
            + fresh * 0.10
        )
        scored.append(
            {
                "card_id": card_id,
                "score": round(total, 4),
                "semantic": round(semantic, 4),
                "semantic_effective": round(semantic_effective, 4),
                "action_match": action_score,
                "priority": priority_score(str(card.get("priority", "中"))),
                "freshness": round(fresh, 4),
                "chain_depth": chain_depth,
                "inject_position": card["inject_position"],
                "inject_order": card["inject_order"],
            }
        )

    scored.sort(key=lambda row: (row["score"], row["inject_order"], row["card_id"]), reverse=True)
    selected = scored[0] if scored else None
    if selected and not director_action and allow_semantic_breakthrough:
        original_semantic = selected["semantic"]
        if original_semantic <= 0.85:
            selected = None
    elif selected and not director_action:
        selected = None

    return {"selected": selected, "ranking": scored, "excluded": excluded}


def trigger_card(state: dict[str, Any], card_id: str, chain_depth: int = 0) -> None:
    can, reason = can_trigger_card(state, card_id)
    if not can:
        raise ValueError(f"剧情卡 {card_id} 不可触发：{reason}")
    card = state["cards"][card_id]
    card["times_triggered"] = int(card.get("times_triggered", 0)) + 1
    card["last_triggered_round"] = state["total_rounds"]
    card["cooldown_until_round"] = state["total_rounds"] + int(card.get("cooldown_rounds", 0))
    sticky_rounds = int(card.get("sticky_rounds", 0))
    card["sticky_until_round"] = state["total_rounds"] + sticky_rounds if sticky_rounds > 0 else None
    card["last_chain_depth"] = chain_depth
    has_dynamic_conditions = bool(card.get("required_conditions") or card.get("required_absent"))
    card["conditions_met"] = not has_dynamic_conditions
    card["condition_first_met_round"] = state["total_rounds"] if not has_dynamic_conditions else None

    if chain_depth < CHAIN_DEPTH_LIMIT:
        for child_id in card.get("chain_triggers", []):
            state["chain_queue"].append(
                {
                    "card_id": child_id,
                    "source_card_id": card_id,
                    "depth": chain_depth + 1,
                    "activate_round": state["total_rounds"] + 1,
                    "expires_round": state["total_rounds"] + CHAIN_QUEUE_TTL,
                }
            )


def prune_card_runtime(state: dict[str, Any]) -> None:
    current = state["total_rounds"]
    state["chain_queue"] = [entry for entry in state["chain_queue"] if int(entry.get("expires_round", -1)) >= current]
    for card in state["cards"].values():
        sticky_until = card.get("sticky_until_round")
        if sticky_until is not None and current > int(sticky_until):
            card["sticky_until_round"] = None


def update_pause(state: dict[str, Any], patch: dict[str, Any]) -> None:
    pause_patch = patch.get("pause")
    if not isinstance(pause_patch, dict):
        return
    level = pause_patch.get("level")
    status = pause_patch.get("status")

    # P1-3.1: 先确定新值，再校验组合
    new_level = level if level is not None else state["pause"]["level"]
    new_status = status if status is not None else state["pause"]["status"]

    if level is not None:
        if level not in VALID_PAUSE_LEVELS:
            raise ValueError(f"未知暂停层级：{level}")
        state["pause"]["level"] = level
    if status is not None:
        if status not in VALID_PAUSE_STATUSES:
            raise ValueError(f"未知暂停状态：{status}")
        state["pause"]["status"] = status

    # P1-3.1: 校验层级+状态组合合法性
    if (new_level, new_status) not in VALID_PAUSE_COMBINATIONS:
        raise ValueError(
            f"非法暂停组合：{new_level}/{new_status}；"
            f"合法组合：L1/L2/L4 必须 PAUSED，L3 必须 RUNNING"
        )

    for key in ("auto_advance_count", "cooldown_threshold"):
        if key in pause_patch:
            state["pause"][key] = max(0, int(pause_patch[key]))


def snapshot_core(state: dict[str, Any]) -> dict[str, Any]:
    excluded = {"checkpoints", "event_log", "idempotency_keys"}
    return {key: copy.deepcopy(value) for key, value in state.items() if key not in excluded}


def create_checkpoint(state: dict[str, Any], label: str, kind: str = "ckpt") -> dict[str, Any]:
    checkpoint_id = f"{kind}_{uuid.uuid4().hex[:8]}"
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "kind": kind,
        "label": label,
        "round": state["total_rounds"],
        "revision": state["revision"],
        "created_at": now_iso(),
        "branch_id": state["timeline"]["current_branch_id"],
        "snapshot": snapshot_core(state),
    }
    state["checkpoints"].append(checkpoint)

    same_kind = [cp for cp in state["checkpoints"] if cp.get("kind") == kind]
    if len(same_kind) > CHECKPOINT_LIMIT_PER_KIND:
        remove_ids = {cp["checkpoint_id"] for cp in same_kind[:-CHECKPOINT_LIMIT_PER_KIND]}
        state["checkpoints"] = [cp for cp in state["checkpoints"] if cp["checkpoint_id"] not in remove_ids]
    return checkpoint


def find_checkpoint(state: dict[str, Any], checkpoint_id: str) -> dict[str, Any]:
    for checkpoint in state["checkpoints"]:
        if checkpoint.get("checkpoint_id") == checkpoint_id:
            return checkpoint
    raise KeyError(f"找不到回滚点：{checkpoint_id}")


def rollback_checkpoint(state: dict[str, Any], checkpoint_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    target = find_checkpoint(state, checkpoint_id)
    checkpoints = copy.deepcopy(state["checkpoints"])
    event_log = copy.deepcopy(state["event_log"])
    idempotency = copy.deepcopy(state["idempotency_keys"])
    session_id = state["session_id"]
    seed = state["seed"]
    current_revision = state["revision"]
    old_branch = state["timeline"]["current_branch_id"]

    safety = create_checkpoint(state, f"回滚前安全备份：{checkpoint_id}", "rollback_safety")
    checkpoints = copy.deepcopy(state["checkpoints"])

    restored = migrate_state(copy.deepcopy(target["snapshot"]))
    restored["session_id"] = session_id
    restored["seed"] = seed
    restored["revision"] = current_revision
    restored["checkpoints"] = checkpoints
    restored["event_log"] = event_log
    restored["idempotency_keys"] = idempotency

    target_branch = target.get("branch_id")
    new_branch = _new_branch_id()
    abandoned = list(restored["timeline"].get("abandoned_branches", []))
    abandoned.append(
        {
            "branch_id": old_branch,
            "abandoned_at": now_iso(),
            "rollback_target": checkpoint_id,
        }
    )
    restored["timeline"]["parent_branch_id"] = target_branch
    restored["timeline"]["current_branch_id"] = new_branch
    restored["timeline"]["abandoned_branches"] = abandoned[-50:]
    return restored, safety


def validate_revision(state: dict[str, Any], expected_revision: Optional[int]) -> None:
    if expected_revision is not None and int(state["revision"]) != int(expected_revision):
        raise RuntimeError(
            f"revision 冲突：期望 {expected_revision}，实际 {state['revision']}。"
            "请重新读取状态后再提交。"
        )


def check_idempotency(state: dict[str, Any], key: Optional[str]) -> bool:
    return bool(key and key in state["idempotency_keys"])


def remember_idempotency(state: dict[str, Any], key: Optional[str]) -> None:
    if not key:
        return
    state["idempotency_keys"].append(key)
    state["idempotency_keys"] = state["idempotency_keys"][-IDEMPOTENCY_LIMIT:]


def _is_resume_patch(patch: dict[str, Any]) -> bool:
    """判断补丁是否是恢复操作（从 L1/L2/L4 恢复到 L3/RUNNING）"""
    pause_patch = patch.get("pause")
    if not isinstance(pause_patch, dict):
        return False
    return (
        pause_patch.get("level") == "L3"
        and pause_patch.get("status") == "RUNNING"
    )


def assert_round_can_advance(state: dict[str, Any], patch: dict[str, Any]) -> None:
    """P0-2.2: 暂停门禁 — L1/L2/L4 暂停时禁止推进剧情，除非是恢复操作"""
    pause = state.get("pause", {})
    level = pause.get("level", "L3")
    status = pause.get("status", "RUNNING")
    blocked = status == "PAUSED" or level in {"L1", "L2", "L4"}
    if blocked and not _is_resume_patch(patch):
        raise RuntimeError(
            f"当前处于 {level}/{status} 暂停状态，禁止推进剧情轮次。"
            f"如需恢复，请在补丁中包含 pause.level=L3, pause.status=RUNNING"
        )


def apply_update(
    state: dict[str, Any],
    patch: dict[str, Any],
    last_action: Optional[str] = None,
    triggered_card: Optional[str] = None,
) -> dict[str, Any]:
    # P0-2.2: 暂停门禁 — 在任何状态变更之前检查
    assert_round_can_advance(state, patch)

    tick_cooldowns(state)
    tick_chekhov_items(state)
    prune_card_runtime(state)

    # P0-2.4: 先推进到新轮次，再记录本轮产生的数据；所有 round 字段语义一致
    state["total_rounds"] += 1

    apply_director_action(state, last_action)
    prune_card_runtime(state)

    metrics = normalize_metrics(patch)
    if metrics.get("branch_id") and metrics["branch_id"] != state["timeline"]["current_branch_id"]:
        raise ValueError("状态补丁 branch_id 与当前分支不一致")
    state["ats_window"].append(metrics)
    state["ats_window"] = state["ats_window"][-WINDOW_SIZE:]

    update_pause(state, patch)

    additions = patch.get("chekhov_add", patch.get("unresolved_chekhov", []))
    if isinstance(additions, (str, dict)):
        additions = [additions]
    for item in additions or []:
        add_or_refresh_chekhov(state, item)

    resolutions = patch.get("chekhov_resolve", patch.get("resolved_chekhov", []))
    if isinstance(resolutions, str):
        resolutions = [resolutions]
    for name in resolutions or []:
        resolve_chekhov(state, str(name))

    conditions = patch.get("card_conditions", {})
    if isinstance(conditions, dict):
        for card_id, met in conditions.items():
            # P1-3.5: 严格校验 JSON boolean
            normalized_met = _require_json_bool(met, f"card_conditions.{card_id}")
            update_card_condition(state, str(card_id), normalized_met)

    if triggered_card:
        trigger_card(state, triggered_card, int(patch.get("chain_depth", 0)))

    shifts = metrics.get("npc_attitude_shifts")
    if isinstance(shifts, list):
        for shift in shifts:
            state["npc_attitude_log"].append(
                {"round": state["total_rounds"], "branch_id": state["timeline"]["current_branch_id"], "data": shift}
            )
        state["npc_attitude_log"] = state["npc_attitude_log"][-200:]

    trends = analyze_trends(state["ats_window"])
    flow = determine_flow_state(trends, state["ats_window"])
    if flow == state["flow_state"]:
        state["flow_rounds_in_state"] += 1
    else:
        state["flow_state"] = flow
        state["flow_rounds_in_state"] = 1

    return build_snapshot(state, metrics, trends)


def build_snapshot(state: dict[str, Any], metrics: Optional[dict[str, Any]] = None, trends: Optional[dict[str, str]] = None) -> dict[str, Any]:
    metrics = metrics or (state["ats_window"][-1] if state["ats_window"] else {})
    trends = trends or analyze_trends(state["ats_window"])
    director = state["director"]
    unresolved = [item for item in state["chekhov_items"].values() if item.get("status") == "unresolved"]
    sticky = []
    delay = []
    for card_id, card in state["cards"].items():
        if card.get("sticky_until_round") is not None:
            sticky.append({"card_id": card_id, "until_round": card["sticky_until_round"]})
        if card.get("condition_first_met_round") is not None and int(card.get("delay_rounds", 0)) > 0:
            delay.append(
                {
                    "card_id": card_id,
                    "first_met_round": card["condition_first_met_round"],
                    "ready_round": int(card["condition_first_met_round"]) + int(card["delay_rounds"]),
                    "mode": card["delay_mode"],
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "revision": state["revision"],
        "session_id": state["session_id"],
        "round": state["total_rounds"],
        "branch_id": state["timeline"]["current_branch_id"],
        "flow_state": state["flow_state"],
        "flow_rounds_in_state": state["flow_rounds_in_state"],
        "current_values": {
            key: metrics.get(key, default)
            for key, default in (
                ("scene_intensity", 5),
                ("chaos_proximity", 5),
                ("player_agency", 5),
                ("goal_progress", 50),
            )
        },
        "trends": trends,
        "pause": copy.deepcopy(state["pause"]),
        "director_cooldowns": {
            "can_intervene": can_intervene(state)[0],
            "general_cooldown": director["action_cooldown_remaining"],
            "silence_required": director["consecutive_silence_required"],
            "daily_transition_cooldown": director["daily_transition_cooldown"],
            "push_recycle_cooldown": director["push_recycle_cooldown"],
            "last_action": director["last_action"],
            "last_action_round": director["last_action_round"],
        },
        "chekhov_items": unresolved,
        "high_urgency_chekhov": [item for item in unresolved if item.get("urgency") == "高"],
        "cards_summary": {
            card_id: {
                "times_triggered": card["times_triggered"],
                "can_trigger": can_trigger_card(state, card_id)[0],
                "can_trigger_reason": can_trigger_card(state, card_id)[1],
                "freshness": round(get_card_freshness(state, card_id), 3),
            }
            for card_id, card in state["cards"].items()
        },
        "sticky_active": sticky,
        "delay_waiting": delay,
        "chain_queue": copy.deepcopy(state["chain_queue"]),
    }


def read_stdin_json_or_text() -> str:
    return sys.stdin.read().strip()


def output_json(data: Any) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_init(args: argparse.Namespace) -> None:
    state_file = args.state_file
    with state_lock(state_file):
        if state_file.exists() and not args.force:
            raise FileExistsError(f"状态文件已存在：{state_file}；使用 --force 覆盖")
        state = init_state(args.session_id, args.seed)
        atomic_save_state(state_file, state)
    output_json({"ok": True, "state_file": str(state_file), "session_id": state["session_id"], "seed": state["seed"]})


def cmd_update(args: argparse.Namespace) -> None:
    text = read_stdin_json_or_text()
    patch = parse_patch(text)
    last_action = args.action or args.legacy_action
    triggered_card = args.card or args.legacy_card
    if isinstance(last_action, str) and last_action.lower() in {"none", "null", "静默", "不干预"}:
        last_action = None
    if isinstance(triggered_card, str) and triggered_card.lower() in {"none", "null", "无"}:
        triggered_card = None
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        if check_idempotency(state, args.idempotency_key):
            output_json({"ok": True, "duplicate": True, "revision": state["revision"], "snapshot": build_snapshot(state)})
            return
        snapshot = apply_update(state, patch, last_action, triggered_card)
        remember_idempotency(state, args.idempotency_key)
        commit_state(
            args.state_file,
            state,
            "round_update",
            {"action": last_action, "triggered_card": triggered_card, "idempotency_key": args.idempotency_key},
        )
        snapshot["revision"] = state["revision"]
    output_json({"ok": True, "duplicate": False, "snapshot": snapshot})


def cmd_decide(args: argparse.Namespace) -> None:
    state = load_state(args.state_file, required=True)
    assert state is not None
    output_json({"ok": True, "revision": state["revision"], "decision": choose_director_action(state)})


def cmd_status(args: argparse.Namespace) -> None:
    state = load_state(args.state_file, required=True)
    assert state is not None
    if args.json:
        output_json({"ok": True, "snapshot": build_snapshot(state)})
        return
    snap = build_snapshot(state)
    values = snap["current_values"]
    print("═" * 46)
    print(f"  镜界导演状态 · Round {snap['round']} · rev {snap['revision']}")
    print("═" * 46)
    print(f"  会话: {snap['session_id']}  分支: {snap['branch_id']}")
    print(f"  心流: {snap['flow_state']} ({snap['flow_rounds_in_state']}轮)")
    print(f"  激烈 {values['scene_intensity']} | 失控 {values['chaos_proximity']} | 掌控 {values['player_agency']} | 目标 {values['goal_progress']}%")
    print(f"  暂停: {snap['pause']['level']} / {snap['pause']['status']}")
    print(f"  上次动作: {snap['director_cooldowns']['last_action'] or '无'}")
    print(f"  通用冷却: {snap['director_cooldowns']['general_cooldown']} | 强制静默: {snap['director_cooldowns']['silence_required']}")
    print(f"  未回收伏笔: {len(snap['chekhov_items'])} | 卡片: {len(snap['cards_summary'])}")
    print("═" * 46)


def cmd_register_cards(args: argparse.Namespace) -> None:
    text = read_stdin_json_or_text()
    data = json.loads(text)
    specs = data if isinstance(data, list) else [data]
    if not all(isinstance(spec, dict) for spec in specs):
        raise ValueError("card-register 需要 JSON object 或 object array")
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        ids = register_cards(state, specs)
        commit_state(args.state_file, state, "cards_registered", {"card_ids": ids})
    output_json({"ok": True, "registered": ids, "revision": state["revision"]})


def cmd_card_condition(args: argparse.Namespace) -> None:
    met = args.value == "met"
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        update_card_condition(state, args.card_id, met)
        commit_state(args.state_file, state, "card_condition", {"card_id": args.card_id, "met": met})
    output_json({"ok": True, "card_id": args.card_id, "met": met, "revision": state["revision"]})



def cmd_card_conditions(args: argparse.Namespace) -> None:
    payload = json.loads(read_stdin_json_or_text())
    if not isinstance(payload, dict):
        raise ValueError("card-conditions 需要 JSON object：{card_id: true/false}")
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        updated: dict[str, bool] = {}
        for card_id, met in payload.items():
            # P1-3.5: 严格校验 JSON boolean
            normalized = _require_json_bool(met, f"card_conditions.{card_id}")
            update_card_condition(state, str(card_id), normalized)
            updated[str(card_id)] = normalized
        commit_state(args.state_file, state, "card_conditions", {"conditions": updated})
    output_json({"ok": True, "updated": updated, "revision": state["revision"]})


def cmd_pause_set(args: argparse.Namespace) -> None:
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        state["pause"]["level"] = args.level
        state["pause"]["status"] = args.status
        if args.reset_auto_count:
            state["pause"]["auto_advance_count"] = 0
        commit_state(
            args.state_file,
            state,
            "pause_set",
            {"level": args.level, "status": args.status, "reset_auto_count": args.reset_auto_count},
        )
    output_json({"ok": True, "pause": state["pause"], "revision": state["revision"]})

def cmd_card_select(args: argparse.Namespace) -> None:
    payload = json.loads(read_stdin_json_or_text())
    if not isinstance(payload, dict):
        raise ValueError("card-select 需要 JSON object")
    state = load_state(args.state_file, required=True)
    assert state is not None
    output_json({"ok": True, "revision": state["revision"], "result": select_card(state, payload)})


def cmd_card_trigger(args: argparse.Namespace) -> None:
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        trigger_card(state, args.card_id, args.chain_depth)
        commit_state(args.state_file, state, "card_triggered", {"card_id": args.card_id, "chain_depth": args.chain_depth})
    output_json({"ok": True, "card_id": args.card_id, "revision": state["revision"], "snapshot": build_snapshot(state)})


def cmd_checkpoint_create(args: argparse.Namespace) -> None:
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        checkpoint = create_checkpoint(state, args.label, args.kind)
        commit_state(args.state_file, state, "checkpoint_created", {"checkpoint_id": checkpoint["checkpoint_id"]})
    output_json({"ok": True, "revision": state["revision"], "checkpoint": {k: v for k, v in checkpoint.items() if k != "snapshot"}})


def cmd_checkpoint_list(args: argparse.Namespace) -> None:
    state = load_state(args.state_file, required=True)
    assert state is not None
    checkpoints = [
        {k: v for k, v in checkpoint.items() if k != "snapshot"}
        for checkpoint in state["checkpoints"]
        if args.kind is None or checkpoint.get("kind") == args.kind
    ]
    output_json({"ok": True, "revision": state["revision"], "checkpoints": checkpoints})


def cmd_checkpoint_preview(args: argparse.Namespace) -> None:
    state = load_state(args.state_file, required=True)
    assert state is not None
    checkpoint = find_checkpoint(state, args.checkpoint_id)
    snap = checkpoint["snapshot"]
    preview = {
        "checkpoint_id": checkpoint["checkpoint_id"],
        "kind": checkpoint["kind"],
        "label": checkpoint["label"],
        "round": checkpoint["round"],
        "branch_id": checkpoint["branch_id"],
        "flow_state": snap.get("flow_state"),
        "pause": snap.get("pause"),
        "chekhov_unresolved": [
            name for name, item in snap.get("chekhov_items", {}).items() if item.get("status") == "unresolved"
        ],
        "cards_count": len(snap.get("cards", {})),
    }
    output_json({"ok": True, "preview": preview})


def cmd_checkpoint_rollback(args: argparse.Namespace) -> None:
    with state_lock(args.state_file):
        state = load_state(args.state_file, required=True)
        assert state is not None
        validate_revision(state, args.expected_revision)
        restored, safety = rollback_checkpoint(state, args.checkpoint_id)
        commit_state(
            args.state_file,
            restored,
            "checkpoint_rollback",
            {"target": args.checkpoint_id, "safety": safety["checkpoint_id"], "new_branch": restored["timeline"]["current_branch_id"]},
        )
    output_json(
        {
            "ok": True,
            "revision": restored["revision"],
            "rolled_back_to": args.checkpoint_id,
            "safety_checkpoint": safety["checkpoint_id"],
            "new_branch_id": restored["timeline"]["current_branch_id"],
            "snapshot": build_snapshot(restored),
        }
    )


def cmd_clean(args: argparse.Namespace) -> None:
    if not args.yes:
        raise ValueError("clean 是破坏性操作，请加 --yes")
    with state_lock(args.state_file):
        if args.state_file.exists():
            args.state_file.unlink()
    output_json({"ok": True, "deleted": str(args.state_file)})


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="镜界 v2.6 导演状态运行时")
    parser.add_argument(
        "--state-file",
        type=lambda value: Path(value).expanduser().resolve(),
        default=default_state_file(),
        help="状态文件路径；也可使用 JINGJIE_STATE_FILE",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="初始化会话")
    p_init.add_argument("--session-id")
    p_init.add_argument("--seed", help="可复现随机种子")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_update = sub.add_parser("update", help="从 stdin 读取 JSON/旧版 @@s 并更新一轮")
    p_update.add_argument("legacy_action", nargs="?", help="兼容旧版位置参数：导演动作")
    p_update.add_argument("legacy_card", nargs="?", help="兼容旧版位置参数：触发卡片")
    p_update.add_argument("--action")
    p_update.add_argument("--card")
    p_update.add_argument("--expected-revision", type=int)
    p_update.add_argument("--idempotency-key")
    p_update.set_defaults(func=cmd_update)

    p_decide = sub.add_parser("decide", help="输出可复现的导演决策，不修改状态")
    p_decide.set_defaults(func=cmd_decide)

    p_status = sub.add_parser("status", help="查看状态")
    p_status.add_argument("--json", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_register = sub.add_parser("card-register", help="从 stdin 注册 JSON 剧情卡")
    p_register.add_argument("--expected-revision", type=int)
    p_register.set_defaults(func=cmd_register_cards)

    p_condition = sub.add_parser("card-condition", help="更新卡片条件是否满足")
    p_condition.add_argument("card_id")
    p_condition.add_argument("value", choices=["met", "unmet"])
    p_condition.add_argument("--expected-revision", type=int)
    p_condition.set_defaults(func=cmd_card_condition)


    p_conditions = sub.add_parser("card-conditions", help="从 stdin 批量更新卡片条件")
    p_conditions.add_argument("--expected-revision", type=int)
    p_conditions.set_defaults(func=cmd_card_conditions)

    p_pause = sub.add_parser("pause-set", help="不推进轮次地修改暂停状态")
    p_pause.add_argument("level", choices=sorted(VALID_PAUSE_LEVELS))
    p_pause.add_argument("status", choices=["RUNNING", "PAUSED"])
    p_pause.add_argument("--reset-auto-count", action="store_true")
    p_pause.add_argument("--expected-revision", type=int)
    p_pause.set_defaults(func=cmd_pause_set)

    p_select = sub.add_parser("card-select", help="从 stdin 接收语义分数并选择卡片")
    p_select.set_defaults(func=cmd_card_select)

    p_trigger = sub.add_parser("card-trigger", help="提交一次卡片触发")
    p_trigger.add_argument("card_id")
    p_trigger.add_argument("--chain-depth", type=int, default=0)
    p_trigger.add_argument("--expected-revision", type=int)
    p_trigger.set_defaults(func=cmd_card_trigger)

    p_cp_create = sub.add_parser("checkpoint-create", help="创建完整状态快照")
    p_cp_create.add_argument("label")
    p_cp_create.add_argument("--kind", default="ckpt")
    p_cp_create.add_argument("--expected-revision", type=int)
    p_cp_create.set_defaults(func=cmd_checkpoint_create)

    p_cp_list = sub.add_parser("checkpoint-list", help="列出快照")
    p_cp_list.add_argument("--kind")
    p_cp_list.set_defaults(func=cmd_checkpoint_list)

    p_cp_preview = sub.add_parser("checkpoint-preview", help="预览快照")
    p_cp_preview.add_argument("checkpoint_id")
    p_cp_preview.set_defaults(func=cmd_checkpoint_preview)

    p_cp_rollback = sub.add_parser("checkpoint-rollback", help="回滚并创建新分支")
    p_cp_rollback.add_argument("checkpoint_id")
    p_cp_rollback.add_argument("--expected-revision", type=int)
    p_cp_rollback.set_defaults(func=cmd_checkpoint_rollback)

    p_clean = sub.add_parser("clean", help="删除状态文件")
    p_clean.add_argument("--yes", action="store_true")
    p_clean.set_defaults(func=cmd_clean)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
        return 0
    except (FileNotFoundError, FileExistsError, KeyError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        output_json({"ok": False, "error": str(exc), "error_type": type(exc).__name__})
        return 2
    except KeyboardInterrupt:
        output_json({"ok": False, "error": "操作已中断", "error_type": "KeyboardInterrupt"})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
