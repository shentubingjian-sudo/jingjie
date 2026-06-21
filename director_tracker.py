#!/usr/bin/env python3
"""
镜界 v2 导演状态簿记脚本
===========================
职责：纯数据维护，不做任何决策。
- 解析每轮 @@s 隐藏层数据
- 维护 8 轮滑动窗口
- 追踪所有冷却计数器（不丢数）
- 追踪契诃夫道具的未使用轮次
- 追踪剧情卡片触发历史
- 计算窗口趋势和心流状态
- 输出状态快照 → AI 拿着快照按 SKILL.md 规则做决策

原则：程序管数，AI 管判断。
"""

import json
import os
import sys
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 配置 ──────────────────────────────────────────────

STATE_FILE = Path(__file__).parent / "director_state.json"
WINDOW_SIZE = 8
L3_COOLDOWN_GENERAL = 2       # 导演通用冷却轮数
L3_CONSECUTIVE_MAX = 3        # 最多连续干预次数
L3_CONSECUTIVE_SILENCE = 3    # 连干预超限后强制静默轮数
L3_DAILY_COOLDOWN = 5         # "日常过渡"冷却
L3_RECYCLE_COOLDOWN = 3       # "推动回收"冷却

# ── 状态初始化 ──────────────────────────────────────────

def init_state(session_id: str = None) -> dict:
    """创建全新的导演状态文件"""
    return {
        "session_id": session_id or datetime.now().strftime("%Y%m%d_%H%M%S"),
        "started_at": datetime.now().isoformat(),
        "total_rounds": 0,
        "state_version": "v2.0",
        "ats_window": [],                    # 最近 WINDOW_SIZE 轮的 @@s 数据
        "flow_state": "沉浸中",              # 当前心流状态
        "flow_rounds_in_state": 0,           # 在当前心流状态持续了多少轮
        "director": {
            "last_action": None,             # 上次导演动作（动作名/None）
            "last_action_round": None,       # 上次动作所在轮次
            "action_cooldown_remaining": 0,  # 通用冷却剩余轮数
            "consecutive_interventions": 0,  # 连续干预次数
            "consecutive_silence_required": 0, # 强制静默剩余轮数
            "daily_transition_cooldown": 0,  # "日常过渡"冷却剩余
            "push_recycle_cooldown": 0,      # "推动回收"冷却剩余
            "intervention_history": []       # 最近几轮的动作记录（用于防抽搐）
        },
        "chekhov_items": [],                 # 契诃夫道具追踪
        "cards": {},                         # 剧情卡片追踪
        "npc_attitude_log": []               # NPC 态度变化日志
    }

# ── 状态读写 ──────────────────────────────────────────

def load_state() -> dict:
    """加载状态文件，不存在则返回 None"""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return None

def save_state(state: dict):
    """持久化状态到文件"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# ── @@s 解析 ─────────────────────────────────────────

def parse_ats(yaml_text: str) -> dict:
    """从 @@s 隐藏层的 YAML 文本中解析结构化数据"""
    data = {}
    lines = yaml_text.strip().split("\n")
    
    current_key = None
    current_list = []
    current_dict = {}
    in_list = False
    in_dict = False
    
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        
        # 顶层键值对
        if ":" in stripped and not stripped.startswith(" ") and not stripped.startswith("-"):
            in_list = False
            in_dict = False
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            
            if val == "":
                # 可能是列表或子字典的开始
                current_key = key
                data[key] = None
            else:
                # 简单数值或字符串
                try:
                    data[key] = int(val)
                except ValueError:
                    try:
                        data[key] = float(val)
                    except ValueError:
                        data[key] = val
                current_key = key
        
        # 列表项
        elif stripped.startswith("- ") and current_key:
            in_list = True
            if data[current_key] is None:
                data[current_key] = []
            item = stripped[2:].strip().strip('"').strip("'")
            data[current_key].append(item)
    
    return data


def parse_ats_structured(yaml_text: str) -> dict:
    """增强版 @@s 解析：处理嵌套结构（npc_attitude_shifts 等）"""
    data = {
        "scene_intensity": 5,
        "chaos_proximity": 5,
        "player_agency": 5,
        "goal_progress": 50,
        "active_npcs": {},
        "npc_attitude_shifts": [],
        "unresolved_chekhov": [],
        "pending_flags": [],
        "npc_reflections": []
    }
    
    lines = yaml_text.strip().split("\n")
    
    # 简单键值对解析
    simple_keys = {"scene_intensity", "chaos_proximity", "player_agency", "goal_progress"}
    
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped == "@@s":
            continue
        
        # 跳过嵌套结构（由更精细的解析处理）
        if stripped.startswith("- ") and ("character:" in stripped or "item:" in stripped):
            continue
        
        if ":" in stripped and not stripped.startswith("  "):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            
            if key in simple_keys and val:
                try:
                    data[key] = int(val)
                except ValueError:
                    pass
    
    # 简单提取 unresolved_chekhov 和 npc_attitude_shifts 的数量
    # （完整解析可由 AI 在 @@s 输出时提供更结构化的格式）
    
    return data


# ── 窗口趋势分析 ──────────────────────────────────────

def analyze_trends(ats_window: list) -> dict:
    """分析 8 轮滑动窗口的四维趋势"""
    if len(ats_window) < 4:
        return {
            "scene_intensity_trend": "→",
            "chaos_proximity_trend": "→",
            "player_agency_trend": "→",
            "goal_progress_trend": "→"
        }
    
    trends = {}
    keys = ["scene_intensity", "chaos_proximity", "player_agency", "goal_progress"]
    trend_labels = {
        "scene_intensity": "scene_intensity_trend",
        "chaos_proximity": "chaos_proximity_trend",
        "player_agency": "player_agency_trend",
        "goal_progress": "goal_progress_trend"
    }
    
    # 取前 N/2 轮和后 N/2 轮
    mid = len(ats_window) // 2
    early = ats_window[:mid]
    recent = ats_window[mid:]
    
    for key in keys:
        early_avg = sum(r.get(key, 5) for r in early) / len(early)
        recent_avg = sum(r.get(key, 5) for r in recent) / len(recent)
        diff = recent_avg - early_avg
        
        if diff > 1.0:
            trends[trend_labels[key]] = "↑"
        elif diff < -1.0:
            trends[trend_labels[key]] = "↓"
        else:
            trends[trend_labels[key]] = "→"
    
    return trends


def determine_flow_state(trends: dict, ats_window: list) -> str:
    """根据趋势和数值判定心流状态"""
    if len(ats_window) < 4:
        return "沉浸中"
    
    latest = ats_window[-1]
    si = latest.get("scene_intensity", 5)
    cp = latest.get("chaos_proximity", 5)
    pa = latest.get("player_agency", 5)
    gp = latest.get("goal_progress", 50)
    
    si_t = trends.get("scene_intensity_trend", "→")
    cp_t = trends.get("chaos_proximity_trend", "→")
    pa_t = trends.get("player_agency_trend", "→")
    gp_t = trends.get("goal_progress_trend", "→")
    
    # 🔴 趋近焦虑：失控度持续上升且掌控度持续下降
    if cp_t == "↑" and pa_t == "↓" and cp >= 6:
        return "趋近焦虑"
    
    # 🟡 趋近无聊：激烈度持续下降且目标无进展
    if si_t == "↓" and gp_t == "→" and si <= 4:
        return "趋近无聊"
    
    # 🟠 失去方向：目标停滞6轮+
    if gp_t == "→" and pa_t == "→" and gp <= 30:
        # 检查前面几轮是否也停滞
        recent_gp = [r.get("goal_progress", 50) for r in ats_window[-4:]]
        if max(recent_gp) - min(recent_gp) <= 10:
            return "失去方向"
    
    # ⚪ 高能后回落：激烈度快速下降且掌控度上升
    if si_t == "↓" and pa_t == "↑":
        prev_si = [r.get("scene_intensity", 5) for r in ats_window[-6:-3]]
        if prev_si and max(prev_si) >= 7:
            return "高能后回落"
    
    # 🟢 沉浸中：默认状态
    return "沉浸中"


# ── 导演冷却管理 ──────────────────────────────────────

def tick_cooldowns(state: dict):
    """每轮推进所有冷却计数器 -1（最少到 0）"""
    d = state["director"]
    d["action_cooldown_remaining"] = max(0, d["action_cooldown_remaining"] - 1)
    d["consecutive_silence_required"] = max(0, d["consecutive_silence_required"] - 1)
    d["daily_transition_cooldown"] = max(0, d["daily_transition_cooldown"] - 1)
    d["push_recycle_cooldown"] = max(0, d["push_recycle_cooldown"] - 1)

def apply_director_action(state: dict, action: str):
    """应用导演动作，设置对应的冷却"""
    d = state["director"]
    d["last_action"] = action
    d["last_action_round"] = state["total_rounds"]
    d["action_cooldown_remaining"] = L3_COOLDOWN_GENERAL
    d["consecutive_interventions"] += 1
    d["intervention_history"].append({
        "round": state["total_rounds"],
        "action": action
    })
    # 只保留最近 5 轮记录
    if len(d["intervention_history"]) > 5:
        d["intervention_history"] = d["intervention_history"][-5:]
    
    if action == "日常过渡":
        d["daily_transition_cooldown"] = L3_DAILY_COOLDOWN
    if action == "推动回收":
        d["push_recycle_cooldown"] = L3_RECYCLE_COOLDOWN
    
    # 连续干预超限
    if d["consecutive_interventions"] >= L3_CONSECUTIVE_MAX:
        d["consecutive_silence_required"] = L3_CONSECUTIVE_SILENCE
        d["consecutive_interventions"] = 0

def can_intervene(state: dict, action: str) -> tuple:
    """检查导演是否可以干预。返回 (can, reason)"""
    d = state["director"]
    
    # 强制静默
    if d["consecutive_silence_required"] > 0:
        return False, f"连续干预超限，强制静默 {d['consecutive_silence_required']} 轮"
    
    # 通用冷却
    if d["action_cooldown_remaining"] > 0:
        return False, f"通用冷却中，{d['action_cooldown_remaining']} 轮后可用"
    
    # 特定动作冷却
    if action == "日常过渡" and d["daily_transition_cooldown"] > 0:
        return False, f"日常过渡冷却中，{d['daily_transition_cooldown']} 轮后可用"
    if action == "推动回收" and d["push_recycle_cooldown"] > 0:
        return False, f"推动回收冷却中，{d['push_recycle_cooldown']} 轮后可用"
    
    # 防抽搐：同一场景内升压和降压不能连续交替
    history = d["intervention_history"]
    if len(history) >= 2:
        last_two = [h["action"] for h in history[-2:]]
        if last_two[-1] == "升压" and action == "降压":
            return False, "防抽搐：上一轮刚升压，本轮不能降压"
        if last_two[-1] == "降压" and action == "升压":
            return False, "防抽搐：上一轮刚降压，本轮不能升压"
    
    return True, "可以干预"


# ── 契诃夫道具追踪 ────────────────────────────────────

def update_chekhov_items(state: dict, unresolved: list):
    """更新契诃夫道具状态：已知道具 rounds_unused+1，新道具加入"""
    existing_names = {item["name"] for item in state["chekhov_items"]}
    
    for item_data in unresolved:
        name = item_data.get("item", item_data.get("name", ""))
        if not name:
            continue
        
        if name in existing_names:
            # 已有道具：更新未使用轮次
            for item in state["chekhov_items"]:
                if item["name"] == name:
                    if item["status"] == "unresolved":
                        item["rounds_unused"] += 1
                        # 更新紧迫度
                        if item["rounds_unused"] >= 10:
                            item["urgency"] = "高"
                        elif item["rounds_unused"] >= 5:
                            item["urgency"] = "中"
                    break
        else:
            # 新道具
            state["chekhov_items"].append({
                "name": name,
                "acquired_round": state["total_rounds"],
                "rounds_unused": 0,
                "urgency": "低",
                "status": "unresolved"
            })

def mark_chekhov_resolved(state: dict, item_name: str):
    """标记道具已回收"""
    for item in state["chekhov_items"]:
        if item["name"] == item_name:
            item["status"] = "resolved"
            item["resolved_round"] = state["total_rounds"]
            break

# ── 剧情卡片追踪 ──────────────────────────────────────

def register_card(state: dict, card_id: str, cooldown_rounds: int, max_triggers: int = 3):
    """注册新卡片"""
    state["cards"][card_id] = {
        "times_triggered": 0,
        "last_triggered_round": None,
        "cooldown_rounds": cooldown_rounds,
        "cooldown_until_round": None,
        "max_triggers": max_triggers
    }

def can_trigger_card(state: dict, card_id: str) -> tuple:
    """检查卡片是否可以触发"""
    if card_id not in state["cards"]:
        return True, "未注册卡片，默认可触发"
    
    card = state["cards"][card_id]
    
    # 达到最大触发次数
    if card["max_triggers"] > 0 and card["times_triggered"] >= card["max_triggers"]:
        return False, f"已达最大触发次数 {card['max_triggers']}"
    
    # 冷却中
    if card["cooldown_until_round"] and state["total_rounds"] < card["cooldown_until_round"]:
        remaining = card["cooldown_until_round"] - state["total_rounds"]
        return False, f"冷却中，{remaining} 轮后可用"
    
    return True, "可以触发"

def trigger_card(state: dict, card_id: str):
    """记录卡片触发"""
    if card_id not in state["cards"]:
        register_card(state, card_id, cooldown_rounds=5)  # 自动注册
    
    card = state["cards"][card_id]
    card["times_triggered"] += 1
    card["last_triggered_round"] = state["total_rounds"]
    card["cooldown_until_round"] = state["total_rounds"] + card["cooldown_rounds"]

def get_card_freshness(state: dict, card_id: str) -> float:
    """计算卡片新鲜度得分 (0-1)"""
    if card_id not in state["cards"]:
        return 1.0  # 未注册 → 全新
    
    card = state["cards"][card_id]
    if card["times_triggered"] == 0:
        return 1.0
    
    rounds_since = state["total_rounds"] - (card["last_triggered_round"] or 0)
    cooldown = card["cooldown_rounds"]
    if cooldown == 0:
        return 0.5
    return min(1.0, 0.7 + (rounds_since / cooldown) * 0.3)


# ── 主更新流程 ─────────────────────────────────────────

def update(ats_yaml: str, last_action: str = None, triggered_card: str = None) -> dict:
    """
    主更新入口：每轮调用一次。
    
    参数:
        ats_yaml: @@s 隐藏层的 YAML 文本
        last_action: 上轮 AI 选择的导演动作（本轮更新时传入）
        triggered_card: 上轮触发的卡片 ID
    
    返回:
        snapshot: 给 AI 读的状态快照（dict）
    """
    state = load_state()
    if state is None:
        print("⚠️ 状态文件不存在，请先执行 init")
        return None
    
    # 1. 推进冷却计数
    tick_cooldowns(state)
    
    # 2. 如果上轮有导演动作，应用冷却
    if last_action:
        apply_director_action(state, last_action)
    
    # 3. 如果上轮触发了卡片，记录
    if triggered_card:
        trigger_card(state, triggered_card)
    
    # 4. 解析 @@s 数据
    ats_data = parse_ats_structured(ats_yaml)
    
    # 5. 更新窗口
    state["ats_window"].append(ats_data)
    if len(state["ats_window"]) > WINDOW_SIZE:
        state["ats_window"] = state["ats_window"][-WINDOW_SIZE:]
    
    # 6. 更新契诃夫道具
    unresolved = ats_data.get("unresolved_chekhov", [])
    if unresolved:
        update_chekhov_items(state, unresolved)
    
    # 7. 更新轮次
    state["total_rounds"] += 1
    
    # 8. 分析趋势
    trends = analyze_trends(state["ats_window"])
    
    # 9. 判定心流状态
    flow = determine_flow_state(trends, state["ats_window"])
    if flow == state["flow_state"]:
        state["flow_rounds_in_state"] += 1
    else:
        state["flow_state"] = flow
        state["flow_rounds_in_state"] = 1
    
    # 10. 持久化
    save_state(state)
    
    # 11. 构建快照
    snapshot = build_snapshot(state, ats_data, trends)
    return snapshot


def build_snapshot(state: dict, ats_data: dict, trends: dict) -> dict:
    """构建给 AI 读的状态快照"""
    d = state["director"]
    window = state["ats_window"]
    
    snapshot = {
        "round": state["total_rounds"],
        "flow_state": state["flow_state"],
        "flow_rounds_in_state": state["flow_rounds_in_state"],
        
        # 当前 @@s 数值
        "current_values": {
            "scene_intensity": ats_data.get("scene_intensity", 5),
            "chaos_proximity": ats_data.get("chaos_proximity", 5),
            "player_agency": ats_data.get("player_agency", 5),
            "goal_progress": ats_data.get("goal_progress", 50)
        },
        
        # 窗口趋势
        "trends": trends,
        
        # 导演冷却状态
        "director_cooldowns": {
            "can_intervene": d["action_cooldown_remaining"] == 0 and d["consecutive_silence_required"] == 0,
            "general_cooldown": d["action_cooldown_remaining"],
            "silence_required": d["consecutive_silence_required"],
            "daily_transition_cooldown": d["daily_transition_cooldown"],
            "push_recycle_cooldown": d["push_recycle_cooldown"],
            "consecutive_interventions": d["consecutive_interventions"],
            "last_action": d["last_action"],
            "last_action_round": d["last_action_round"]
        },
        
        # 契诃夫道具
        "chekhov_items": [
            item for item in state["chekhov_items"] if item["status"] == "unresolved"
        ],
        "high_urgency_chekhov": [
            item for item in state["chekhov_items"] 
            if item["status"] == "unresolved" and item["urgency"] == "高"
        ],
        
        # 卡片状态
        "cards_summary": {
            card_id: {
                "times_triggered": info["times_triggered"],
                "can_trigger": can_trigger_card(state, card_id)[0],
                "freshness": round(get_card_freshness(state, card_id), 2)
            }
            for card_id, info in state["cards"].items()
        }
    }
    
    return snapshot


# ── 命令行接口 ─────────────────────────────────────────

def cmd_init():
    """初始化：创建新的状态文件"""
    import uuid
    session_id = str(uuid.uuid4())[:8]
    state = init_state(session_id)
    save_state(state)
    print(f"✅ 镜界导演状态已初始化 (session: {session_id})")
    print(f"   状态文件: {STATE_FILE}")

def cmd_update():
    """更新：解析 stdin 中的 @@s YAML，更新状态，输出快照"""
    ats_yaml = sys.stdin.read().strip()
    if not ats_yaml:
        print("❌ 未收到 @@s 数据，请通过 stdin 传入")
        return
    
    # 从命令行参数或环境变量获取上轮信息
    # 用法: python director_tracker.py update [last_action] [triggered_card]
    last_action = None
    triggered_card = None
    
    if len(sys.argv) >= 3:
        last_action = sys.argv[2] if sys.argv[2] != "none" else None
    if len(sys.argv) >= 4:
        triggered_card = sys.argv[3] if sys.argv[3] != "none" else None
    
    # 环境变量作为备用
    if not last_action:
        last_action = os.environ.get("DIRECTOR_LAST_ACTION")
    if not triggered_card:
        triggered_card = os.environ.get("DIRECTOR_TRIGGERED_CARD")
    
    snapshot = update(ats_yaml, last_action, triggered_card)
    if snapshot:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print("❌ 更新失败")

def cmd_status():
    """读取当前状态并输出可读摘要"""
    state = load_state()
    if state is None:
        print("❌ 状态文件不存在，请先 init")
        return
    
    d = state["director"]
    w = state["ats_window"]
    
    print(f"═══════════════════════════════════")
    print(f"  🎬 导演状态 (Round {state['total_rounds']})")
    print(f"═══════════════════════════════════")
    print(f"  心流状态: {state['flow_state']} ({state['flow_rounds_in_state']}轮)")
    print(f"")
    if w:
        latest = w[-1]
        print(f"  激烈度: {latest.get('scene_intensity', '?')}  "
              f"失控度: {latest.get('chaos_proximity', '?')}  "
              f"掌控度: {latest.get('player_agency', '?')}  "
              f"目标: {latest.get('goal_progress', '?')}%")
    print(f"")
    print(f"  上次动作: {d['last_action'] or '无'} (Round {d['last_action_round'] or '-'})")
    print(f"  通用冷却: {d['action_cooldown_remaining']}轮")
    print(f"  强制静默: {d['consecutive_silence_required']}轮")
    print(f"  连续干预: {d['consecutive_interventions']}次")
    print(f"  日常冷却: {d['daily_transition_cooldown']}轮")
    print(f"  回收冷却: {d['push_recycle_cooldown']}轮")
    print(f"")
    unresolved = [i for i in state["chekhov_items"] if i["status"] == "unresolved"]
    if unresolved:
        print(f"  契诃夫道具 ({len(unresolved)}):")
        for item in unresolved:
            print(f"    - {item['name']}: {item['rounds_unused']}轮未用 (紧迫度:{item['urgency']})")
    print(f"")
    cards = state["cards"]
    if cards:
        print(f"  剧情卡片 ({len(cards)}):")
        for cid, info in cards.items():
            can, reason = can_trigger_card(state, cid)
            status = "可用" if can else reason
            print(f"    - {cid}: 触发{info['times_triggered']}次 | {status}")
    print(f"═══════════════════════════════════")

def cmd_decide():
    """输出 AI 决策所需的完整上下文（快照 + 决策矩阵提示）"""
    state = load_state()
    if state is None:
        print("❌ 状态文件不存在，请先 init")
        return
    
    trends = analyze_trends(state["ats_window"]) if state["ats_window"] else {}
    latest = state["ats_window"][-1] if state["ats_window"] else {}
    
    snapshot = {
        "round": state["total_rounds"],
        "flow_state": state["flow_state"],
        "flow_rounds": state["flow_rounds_in_state"],
        "values": latest,
        "trends": trends,
        "cooldowns": state["director"],
        "chekhov_high_urgency": [
            i["name"] for i in state["chekhov_items"] 
            if i["status"] == "unresolved" and i["urgency"] == "高"
        ]
    }
    
    # 输出快照 + 决策矩阵（AI 拿着这个直接按 SKILL.md 规则做判断）
    print("=" * 50)
    print("📊 导演状态快照")
    print("=" * 50)
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    print()
    print("=" * 50)
    print("🎯 决策矩阵（来自 SKILL.md P2）")
    print("=" * 50)
    print("""
🟢 沉浸中 → 不干预（静默）
🟡 趋近无聊 → 70%升压 | 30%加阻力
🔴 趋近焦虑 → 60%降压 | 40%给突破口
🟠 失去方向 → 50%给突破口 | 50%推动回收
⚪ 高能后回落 → 90%日常过渡 | 10%不干预

例外: 高紧迫度契诃夫道具未收 → 额外+40%权重选"推动回收"
""")

def cmd_clean():
    """删除状态文件"""
    if STATE_FILE.exists():
        STATE_FILE.unlink()
        print("🗑️ 导演状态已清除")
    else:
        print("ℹ️ 状态文件不存在")

# ── 主入口 ────────────────────────────────────────────

USAGE = """
镜界 v2 导演状态簿记脚本

用法:
  python director_tracker.py init      - 初始化新会话
  python director_tracker.py update    - 从 stdin 读取 @@s 并更新状态（输出快照 JSON）
  python director_tracker.py status    - 显示当前状态摘要
  python director_tracker.py decide    - 输出 AI 决策所需的完整上下文
  python director_tracker.py clean     - 清除状态文件

示例:
  echo "@@s
  scene_intensity: 7
  chaos_proximity: 3
  player_agency: 6
  goal_progress: 45" | python director_tracker.py update
"""

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(USAGE)
    else:
        cmd = sys.argv[1]
        if cmd == "init":
            cmd_init()
        elif cmd == "update":
            cmd_update()
        elif cmd == "status":
            cmd_status()
        elif cmd == "decide":
            cmd_decide()
        elif cmd == "clean":
            cmd_clean()
        else:
            print(f"未知命令: {cmd}")
            print(USAGE)
