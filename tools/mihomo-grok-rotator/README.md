# Dedicated Mihomo Grok rotator

`mihomo-grok-rotator` keeps the Grok Build egress on a separate Mihomo
instance. It reads the official Grok2API quality-guard state and changes only
the configured Mihomo selector after confirmed active-probe anomalies.

The rotator does not disable Grok2API egress nodes and does not access the
shared Mihomo instance used by the other applications.

## Deployment topology

```text
Grok2API -> mihomo-grok:7890 -> selected VLESS leaf
                  ^
                  | Controller :9090
          mihomo-grok-rotator <- quality_guard_state/state.json
```

The compose configuration intentionally uses the `🐟 漏网之鱼` selector. The
current subscription routes `cli-chat-proxy.grok.com`, used by Grok Build,
through that selector. Changing a different selector would not change Grok
Build traffic.

## Rotation policy

Only new `active` observations for the one managed Grok Build egress node are
eligible. Passive observations are left to the official guard's active
confirmation step.

| Condition | Default action |
| --- | --- |
| 2 consecutive active `hard` classifications | Rotate |
| 2 consecutive active `soft` classifications | Rotate |
| 2 increasing consecutive probe errors | Rotate |
| Any healthy active classification | Reset soft/hard streaks |

Before changing the selector, the controller skips nodes in its per-node
cooldown and tests up to eight candidates against `cli-chat-proxy.grok.com`.
The former node is cooled for one hour. A global 15-minute cooldown prevents
flapping. The selected leaf is persisted and restored after an `mihomo-grok`
restart.

### Stream-drain protection

When an anomaly is confirmed, the rotator first records a pending rotation.
It queries the dedicated Mihomo controller and delays the selector change
while traffic on a `cli-chat-proxy.grok.com` connection is still moving. HTTP
keep-alive connections remain visible after a request completes, so the
rotator waits for a configurable quiet period instead of waiting for every
connection to disappear. The deployment uses
`ROTATOR_DRAIN_QUIET_SECONDS=120`: two quiet minutes are required before an
already-confirmed rotation may proceed.

This makes the normal flow "finish the current response, then route subsequent
requests through the new node." The pending reason is stored in the rotator
state volume, so a deferred rotation is not lost if the rotator restarts.

## Operations

Logs are JSON lines and intentionally exclude controller credentials and
proxy URLs:

```sh
docker logs -f mihomo-grok-rotator
```

Useful events are `baseline_initialized`, `observation_recorded`,
`rotation_deferred`, `node_rotated`, `rotation_suppressed`, and
`rotation_failed`.

The controller reads the Mihomo API secret from the mounted configuration at
runtime. Do not copy that secret into this repository, compose file, logs, or
an image layer.
