# Local AI notes (Ollama, user-space, loopback only)

Health data must never leave the house. The optional AI morning note and Q&A run on your own PC
with Ollama bound to `127.0.0.1:11434` (`RINGKEEPER_AI_URL`). Reference machine: a mid-range
desktop (8-core Ryzen APU, 6 GB NVIDIA GPU, 16 GB RAM); a 4B model also runs CPU-only, just slower.
`tools/analysis/ai_note.py` refuses any non-loopback URL and only sends derived numbers.

## Install (no sudo)

Or use your distro's package / the official installer. A user-space install:

```sh
V=v0.40.18; D=~/.cache/ollama-dl/$V; mkdir -p $D && cd $D
curl -fLO https://github.com/ollama/ollama/releases/download/$V/sha256sum.txt
curl -fLO https://github.com/ollama/ollama/releases/download/$V/ollama-linux-amd64.tar.zst
grep ' ./ollama-linux-amd64.tar.zst$' sha256sum.txt; sha256sum ollama-linux-amd64.tar.zst   # must match
# v0.40.18: b82649006cb5b54ed1237fa2d1a26a7f939a4fd03150033759acbff7ff315adf (also the GitHub asset digest)
P=~/.local/opt/ollama-${V#v}; mkdir -p $P && tar --zstd -xf ollama-linux-amd64.tar.zst -C $P --no-same-owner
ln -sfn $P ~/.local/opt/ollama && ln -sf ~/.local/opt/ollama/bin/ollama ~/.local/bin/ollama
```

Upgrade = same steps with a new `V`, then `systemctl --user restart ollama`; old dirs can be trashed.

`~/.config/systemd/user/ollama.service` (enabled, `default.target`):

```ini
[Unit]
Description=Ollama (local LLM, user-space; loopback only) for ringkeeper private notes
After=network.target

[Service]
Type=exec
ExecStart=%h/.local/opt/ollama/bin/ollama serve
Environment=OLLAMA_HOST=127.0.0.1:11434
Environment=OLLAMA_ORIGINS=http://127.0.0.1,http://localhost
Environment=OLLAMA_MODELS=%h/.ollama/models
Environment=OLLAMA_NO_CLOUD=1
Environment=OLLAMA_KEEP_ALIVE=10m
Environment=OLLAMA_NUM_PARALLEL=1
Environment=OLLAMA_MAX_LOADED_MODELS=1
Environment=OLLAMA_FLASH_ATTENTION=1
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes

[Install]
WantedBy=default.target
```

Note: as a `--user` service it runs while your user manager is up (login session, or always if
lingering is enabled — `loginctl enable-linger` needs no sudo for your own user).

## Model: `qwen3:4b-instruct-2507-q4_K_M`

| candidate | file | why / why not |
| --- | --- | --- |
| **qwen3 4B instruct-2507 Q4_K_M** | 2.5 GB | chosen: strongest ≤4B instruction follower in the library for terse, rule-bound text; non-thinking variant (no `<think>` overhead); 3.2 GB loaded at 4k ctx → 100 % GPU with ~2 GB VRAM spare |
| llama3.2 3B | 2.0 GB | older, weaker at "use only these numbers" |
| qwen2.5 3B instruct | 1.9 GB | superseded by qwen3 2507 |
| qwen3.5 4B | 3.3 GB | newer, but thinking-by-default + vision projector; more VRAM for no gain on 4-line notes |
| gemma4 e4b / gemma3 4B | 3.3 GB+ | nice prose, larger footprint; worth an A/B later |

Measured on the reference machine (ollama 0.40.18, CUDA 13 runner, driver 615.78): `ollama ps` → `100% GPU`,
3.2 GB, ctx 4096; ~52–57 tokens/s generation; a note takes ~1.3 s warm, ~18 s cold (model load);
`nvidia-smi` ~3.6 GB used of 6 GB while loaded (incl. ~0.4 GB desktop). Unloads after 10 min idle.

Switch model: `ollama pull <tag>` then `RINGKEEPER_AI_MODEL=<tag>` (env) or `--model`.

## Use

```sh
tools/morning_sync.sh --ai                          # sync + analysis + AI note + export (default: no AI)
python3 -I tools/analysis/ai_note.py                # note -> stdout + data/ai_note.json
python3 -I tools/analysis/ai_note.py context        # exactly what the model sees
python3 -I tools/analysis/ai_note.py ask "How much deep sleep did I get?"   [--json]
```

Live chat: the dashboard's AI tab (Real mode) posts to `/api/ask` on the same origin, served by
`tools/dash_api.py` (`ringkeeper-dashboard.service`, 127.0.0.1:8091 → tailscale serve :8443). Same
context, same output checks as `ask`; rate-limited (6/min, 60/day, one at a time), questions ≤ 300
chars and never logged. See tools/analysis/README.md § Dashboard server.

The dashboard exports `ai_note` (only when `source == "ai"` and it is for the exported night);
the Today card shows it tagged "local AI", else the template note.
