# CardForgeRust (Minenite dedicated plugins)

Astris is the **web Horizon client**. For **dedicated Horizon servers**, Minenite
runs [CardForgeRust](https://github.com/minenite/cardforgerust) — a Bukkit-inspired
Rust plugin host linked as a Horizon native trait mod.

## When multiplayer lands

Astris README notes: *There is no multiplayer yet.* Once Astris can join a
dedicated Horizon server, CardForgeRust is the plugin surface operators use:

| Piece | Role |
| --- | --- |
| `cardforge-host` | Horizon `Hooks` → CardForge events |
| `hello` / `friends` / `ranks` / `travel` | In-tree gameplay plugins |
| `plugins/*.so` | Drop-in dynamic plugins |

Chat cancel works today on the dedicated path; signed chat still cannot rewrite
the broadcast body (Horizon limitation).

## Local dedicated smoke (operators)

See CardForgeRust README for `horizon-launcher native-build` and
`server.toml` `engine.path` wiring.
