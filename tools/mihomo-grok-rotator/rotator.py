#!/usr/bin/env python3
"""Conservative Mihomo selector rotation driven by Grok2API guard state.

The official guard remains the source of quality measurements. This process
only reacts to new active-probe observations, so passive request noise cannot
switch the egress by itself. It intentionally controls a dedicated Mihomo
instance and never mutates Grok2API egress-node availability.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


def env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(minimum, value)


CONTROLLER = os.getenv("MIHOMO_CONTROLLER", "http://mihomo-grok:9090").rstrip("/")
CONFIG_FILE = Path(os.getenv("MIHOMO_CONFIG_FILE", "/etc/mihomo/config.yaml"))
SELECTOR = os.getenv("MIHOMO_SELECTOR", "🚀 节点选择").strip()
TARGET_URL = os.getenv("MIHOMO_TARGET_URL", "https://console.x.ai").strip()
GUARD_STATE = Path(os.getenv("QUALITY_GUARD_STATE_FILE", "/var/lib/grok2api-quality-guard/state.json"))
STATE_FILE = Path(os.getenv("ROTATOR_STATE_FILE", "/var/lib/mihomo-grok-rotator/state.json"))
POLL_SECONDS = env_int("ROTATOR_POLL_SECONDS", 15)
HARD_REQUIRED = env_int("ROTATOR_HARD_REQUIRED", 2)
SOFT_REQUIRED = env_int("ROTATOR_SOFT_REQUIRED", 2)
ERROR_REQUIRED = env_int("ROTATOR_ERROR_REQUIRED", 2)
GLOBAL_COOLDOWN = env_int("ROTATOR_GLOBAL_COOLDOWN_SECONDS", 900)
NODE_COOLDOWN = env_int("ROTATOR_NODE_COOLDOWN_SECONDS", 3600)
MAX_CANDIDATES = env_int("ROTATOR_MAX_CANDIDATES", 8)


def log(event: str, **fields: Any) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **fields}, ensure_ascii=False), flush=True)


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else default
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp = STATE_FILE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(STATE_FILE)


def default_state() -> dict[str, Any]:
    return {
        "version": 1,
        "initialized": False,
        "last_observed_at": 0.0,
        "last_error_strikes": 0,
        "soft_streak": 0,
        "hard_streak": 0,
        "last_rotation_at": 0.0,
        "last_selected": "",
        "node_cooldowns": {},
    }


def controller_secret() -> str:
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"mihomo_config_unreadable:{type(exc).__name__}") from exc
    match = re.search(r"(?m)^secret:\s*(?:[\"']([^\"']+)[\"']|([^\s#]+))\s*$", text)
    if not match:
        raise RuntimeError("mihomo_controller_secret_missing")
    return (match.group(1) or match.group(2) or "").strip()


def request(method: str, path: str, body: dict[str, Any] | None = None, timeout: int = 20) -> Any:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {controller_secret()}"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(CONTROLLER + path, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw else {}


def proxy_map() -> dict[str, dict[str, Any]]:
    data = request("GET", "/proxies")
    values = data.get("proxies", {}) if isinstance(data, dict) else {}
    return values if isinstance(values, dict) else {}


def eligible_leaves(values: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    group = values.get(SELECTOR)
    if not isinstance(group, dict):
        raise RuntimeError("selector_not_found")
    current = str(group.get("now") or "")
    candidates = []
    for name in group.get("all", []):
        item = values.get(str(name), {})
        if str(item.get("type") or "").lower() == "vless":
            candidates.append(str(name))
    if not candidates:
        raise RuntimeError("selector_has_no_vless_nodes")
    return current, candidates


def is_healthy_candidate(name: str) -> bool:
    path = "/proxies/" + urllib.parse.quote(name, safe="") + "/delay?timeout=10000&url=" + urllib.parse.quote(TARGET_URL, safe="")
    try:
        result = request("GET", path, timeout=15)
    except Exception:
        return False
    return isinstance(result, dict) and isinstance(result.get("delay"), (int, float)) and result.get("delay", -1) >= 0


def rotate(state: dict[str, Any], reason: str) -> bool:
    now = time.time()
    if now - float(state.get("last_rotation_at") or 0) < GLOBAL_COOLDOWN:
        log("rotation_suppressed", reason=reason, suppression="global_cooldown")
        return False
    values = proxy_map()
    current, candidates = eligible_leaves(values)
    cooldowns = {str(k): float(v) for k, v in (state.get("node_cooldowns") or {}).items()}
    if current:
        cooldowns[current] = now + NODE_COOLDOWN
    start = (candidates.index(current) + 1) % len(candidates) if current in candidates else 0
    selected = ""
    for offset in range(min(MAX_CANDIDATES, len(candidates))):
        candidate = candidates[(start + offset) % len(candidates)]
        if cooldowns.get(candidate, 0.0) > now:
            continue
        if is_healthy_candidate(candidate):
            selected = candidate
            break
    state["node_cooldowns"] = {name: until for name, until in cooldowns.items() if until > now}
    if not selected:
        save_state(state)
        log("rotation_failed", reason=reason, current=current, checked=min(MAX_CANDIDATES, len(candidates)))
        return False
    request("PUT", "/proxies/" + urllib.parse.quote(SELECTOR, safe=""), {"name": selected})
    current_after, _ = eligible_leaves(proxy_map())
    if current_after != selected:
        raise RuntimeError("selector_change_not_confirmed")
    state.update({
        "last_rotation_at": now,
        "last_selected": selected,
        "soft_streak": 0,
        "hard_streak": 0,
        "last_error_strikes": 0,
    })
    save_state(state)
    log("node_rotated", reason=reason, previous=current, selected=selected)
    return True


def restore_last_selection(state: dict[str, Any]) -> None:
    """Restore the last canary-validated leaf after a Mihomo restart."""
    selected = str(state.get("last_selected") or "")
    if not selected:
        return
    values = proxy_map()
    current, candidates = eligible_leaves(values)
    if selected not in candidates:
        log("selection_restore_skipped", reason="saved_node_missing")
        return
    if current == selected:
        return
    request("PUT", "/proxies/" + urllib.parse.quote(SELECTOR, safe=""), {"name": selected})
    confirmed, _ = eligible_leaves(proxy_map())
    if confirmed != selected:
        raise RuntimeError("selector_restore_not_confirmed")
    log("selection_restored")


def observe(state: dict[str, Any], guard: dict[str, Any]) -> None:
    node_ids = [str(value) for value in ((guard.get("guard") or {}).get("node_ids") or [])]
    if len(node_ids) != 1:
        raise RuntimeError("expected_exactly_one_guard_node")
    node = ((guard.get("nodes") or {}).get(node_ids[0]) or {})
    observed_at = float(node.get("last_observed_at") or node.get("last_probe_at") or 0.0)
    error_strikes = int(node.get("error_strikes") or 0)
    if not state.get("initialized"):
        state.update({"initialized": True, "last_observed_at": observed_at, "last_error_strikes": error_strikes})
        save_state(state)
        log("baseline_initialized", guard_node_id=node_ids[0])
        return
    if error_strikes >= ERROR_REQUIRED and error_strikes > int(state.get("last_error_strikes") or 0):
        state["last_error_strikes"] = error_strikes
        rotate(state, "confirmed_probe_errors")
        return
    state["last_error_strikes"] = error_strikes
    if observed_at <= float(state.get("last_observed_at") or 0.0):
        save_state(state)
        return
    state["last_observed_at"] = observed_at
    if str(node.get("last_source") or "") != "active":
        save_state(state)
        return
    classification = str(node.get("last_classification") or "")
    if classification == "hard":
        state["hard_streak"] = int(state.get("hard_streak") or 0) + 1
        state["soft_streak"] = 0
    elif classification == "soft":
        state["soft_streak"] = int(state.get("soft_streak") or 0) + 1
        state["hard_streak"] = 0
    elif classification == "healthy":
        state["soft_streak"] = 0
        state["hard_streak"] = 0
    else:
        save_state(state)
        return
    reason = ""
    if int(state.get("hard_streak") or 0) >= HARD_REQUIRED:
        reason = "confirmed_hard_quality"
    elif int(state.get("soft_streak") or 0) >= SOFT_REQUIRED:
        reason = "confirmed_soft_quality"
    if reason:
        rotate(state, reason)
    else:
        save_state(state)
        log("observation_recorded", classification=classification, soft_streak=state["soft_streak"], hard_streak=state["hard_streak"])


def main() -> None:
    state = load_json(STATE_FILE, default_state())
    log("rotator_started", selector=SELECTOR, hard_required=HARD_REQUIRED, soft_required=SOFT_REQUIRED, error_required=ERROR_REQUIRED)
    restored = False
    while True:
        try:
            # Reload before every cycle so an operator-approved canary
            # selection is never overwritten by an older in-memory snapshot.
            state = load_json(STATE_FILE, state)
            if not restored:
                restore_last_selection(state)
                restored = True
            observe(state, load_json(GUARD_STATE, {}))
        except Exception as exc:
            log("rotator_cycle_failed", error_type=type(exc).__name__)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
