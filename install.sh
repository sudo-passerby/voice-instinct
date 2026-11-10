#!/usr/bin/env bash
# voice-instinct installer: system packages, uv, models, backend, frontend, services.
# Usage: ./install.sh [--dry-run] [--uninstall] [--purge]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOME_DIR="${VI_HOME:-$HOME}"
OS_RELEASE="${VI_OS_RELEASE:-/etc/os-release}"
DRY=0 UNINSTALL=0 PURGE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PKGS=(bubblewrap socat curl tar bzip2)
NODE_VER="22.23.3"  # one pinned official Node: distro Node (Debian 12 has 18) is too old for Electron 44
NODE_DIR="$HOME_DIR/.voice-instinct/node"
case "$(uname -m)" in
  x86_64) NODE_ARCH=x64; NODE_SHA="df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de" ;;
  aarch64) NODE_ARCH=arm64; NODE_SHA="a44aeb94849a299b22df10b9e622ec2f605c2183501bc40590705131de7c740f" ;;
  *) NODE_ARCH="" NODE_SHA="" ;;
esac
MODELS="$HOME_DIR/.voice-instinct/models"
ASR_MODELS="https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"
SPEAKER_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models"  # sic
SPEAKER_MODEL="nemo_en_titanet_small.onnx"
SPEAKER_SHA="ad4a1802485d8b34c722d2a9d04249662f2ece5d28a7a039063ca22f515a789e"
KOKORO_BASE="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
KOKORO_ONNX_SHA="7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5"
KOKORO_VOICES_SHA="bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d"
UNIT="$HOME_DIR/.config/systemd/user/voice-instinct.service"
MEMORY_UNIT="$HOME_DIR/.config/systemd/user/voice-instinct-memory.service"  # the nightly memory run (#26)
MEMORY_TIMER="$HOME_DIR/.config/systemd/user/voice-instinct-memory.timer"
AUTOSTART="$HOME_DIR/.config/autostart/voice-instinct.desktop"
LAUNCHER="$HOME_DIR/.local/bin/voice-instinct"
# engine qwen3-asr-gpu: Qwen3-ASR on the GPU, its own venv and service (backend/qwen_asr_server.py, port 7861)
QWEN=""  # 1 when chosen
QWEN_DIR="$HOME_DIR/.voice-instinct/qwen-asr"
QWEN_MODEL="$MODELS/Qwen3-ASR-1.7B-hf"
QWEN_UNIT="$HOME_DIR/.config/systemd/user/voice-instinct-qwen.service"
QWEN_REV="bcd2b5b7f32b480ab5790554cfa8347f246a14f3"

say() { printf '==> %s\n' "$*"; }
run() { if [ "$DRY" = 1 ]; then printf 'would run: %s\n' "$*"; else "$@"; fi; }
write() { # write <path> <mode>  (content on stdin)
  if [ "$DRY" = 1 ]; then printf 'would write: %s\n' "$1"; cat >/dev/null; return; fi
  mkdir -p "$(dirname "$1")"; cat >"$1"; chmod "$2" "$1"
}

if [ "$(id -u)" = 0 ] && [ "$DRY" = 0 ]; then echo "Run this as your normal user, not root (it uses sudo only for packages)." >&2; exit 1; fi

distro_install() {
  # shellcheck disable=SC1090
  . "$OS_RELEASE"
  local family="${ID:-} ${ID_LIKE:-}" have=1 c
  for c in ${VI_PKG_CHECK:-bwrap socat curl tar bzip2 xz}; do command -v "$c" >/dev/null || have=0; done  # tests override
  if [ "$have" = 1 ]; then say "System packages already installed: bubblewrap socat curl tar bzip2 xz"; return; fi
  case "$family" in
    *arch*) PKGS+=(xz); PM=(sudo pacman -S --needed "${PKGS[@]}") ;;
    *debian*|*ubuntu*) PKGS+=(xz-utils); PM=(sudo apt-get install -y "${PKGS[@]}") ;;
    *fedora*|*rhel*) PKGS+=(xz); PM=(sudo dnf install -y "${PKGS[@]}") ;;
    *) echo "Your distribution (${NAME:-unknown}) is not supported yet. Supported: Arch, Debian/Ubuntu, Fedora." >&2
       echo "Install these yourself, then re-run: ${PKGS[*]} xz" >&2; exit 1 ;;
  esac
  say "System packages (needs sudo): ${PKGS[*]}"
  if [ "$DRY" = 0 ]; then
    ok=n  # no terminal or no answer (CI, curl | bash) means no: never run sudo unasked
    [ -t 0 ] && { read -r -p "Install them now with sudo? [Y/n] " ok || ok=n; ok="${ok:-y}"; }
    case "$ok" in [yY]*) ;; *) echo "Not installing system packages. Run this yourself, then re-run: ${PM[*]}" >&2; exit 1 ;; esac
  fi
  run "${PM[@]}"
}

fetch() { # fetch <url> <dest> <sha256>
  local url="$1" dest="$2" sha="$3"
  if [ -f "$dest" ] && [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$sha" ]; then say "keep $(basename "$dest") (checksum ok)"; return; fi
  run mkdir -p "$(dirname "$dest")"
  run curl -fL --retry 2 -o "$dest.part" "$url"
  if [ "$DRY" = 0 ]; then
    if [ "$(sha256sum "$dest.part" | cut -d' ' -f1)" != "$sha" ]; then rm -f "$dest.part"; echo "Checksum mismatch for $url" >&2; exit 1; fi
    mv "$dest.part" "$dest"
  fi
}

node_install() {
  if [ "$("$NODE_DIR/bin/node" --version 2>/dev/null)" = "v$NODE_VER" ]; then say "keep Node v$NODE_VER"; return; fi
  local tgz="$HOME_DIR/.voice-instinct/node-v$NODE_VER-linux-$NODE_ARCH.tar.xz"
  fetch "https://nodejs.org/dist/v$NODE_VER/node-v$NODE_VER-linux-$NODE_ARCH.tar.xz" "$tgz" "$NODE_SHA"
  run rm -rf "$NODE_DIR"
  run mkdir -p "$NODE_DIR"
  run tar -xJf "$tgz" -C "$NODE_DIR" --strip-components=1
  run rm -f "$tgz"
}

models() {
  if [ -n "$QWEN" ]; then :; elif [ -f "$MODELS/$STT/tokens.txt" ] || [ -f "$MODELS/$STT/${STT#sherpa-onnx-whisper-}-tokens.txt" ]; then say "keep speech model $STT"; else
    fetch "$STT_URL" "$MODELS/$STT.tar.bz2" "$STT_SHA"
    run tar -xjf "$MODELS/$STT.tar.bz2" -C "$MODELS"
    run rm -f "$MODELS/$STT.tar.bz2"
  fi
  fetch "$KOKORO_BASE/kokoro-v1.0.onnx" "$MODELS/kokoro/kokoro-v1.0.onnx" "$KOKORO_ONNX_SHA"
  fetch "$KOKORO_BASE/voices-v1.0.bin" "$MODELS/kokoro/voices-v1.0.bin" "$KOKORO_VOICES_SHA"
  # speaker check (#32): only the enrolled owner's voice is heard (backend/voiceprint.py SPEAKER_MODEL)
  fetch "$SPEAKER_URL/$SPEAKER_MODEL" "$MODELS/$SPEAKER_MODEL" "$SPEAKER_SHA"
}

# cfgval <key>: value from config.toml, else config.example.toml; never fails (no pipefail abort on a missing file)
cfgval() {
  local f v=""
  for f in "$REPO/config.toml" "$REPO/config.example.toml"; do
    [ -f "$f" ] || continue
    v="$(sed -n -e "s/^$1 *= *\"\([^\"]*\)\".*/\1/p" -e "s/^$1 *= *\([0-9][0-9]*\).*/\1/p" "$f" | head -1)"
    [ -n "$v" ] && break
  done
  printf '%s' "$v"
}

# stt_choice: the speech model for config's [stt] engine (backend/services.py STT_MODELS has the same names)
stt_choice() {
  local engine
  engine="$(cfgval engine)"
  case "$engine" in
    nemotron) STT="sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-int8-2026-04-25"
      STT_SHA="0ae73a41cd51599dc7cac9ac083d9d35de53d762ca45923505fde47a3751814b" ;;
    nemotron-multilingual) STT="sherpa-onnx-nemotron-3.5-asr-streaming-0.6b-160ms-int8-2026-06-11"
      STT_SHA="a81909a1780d84cff16d73c15e13e67d9d81d8839faf14870d507d8499f7a61a" ;;
    parakeet) STT="sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8"
      STT_SHA="5793d0fd397c5778d2cf2126994d58e9d56b1be7c04d13c7a15bb1b4eafb16bf" ;;
    whisper-tiny) STT="sherpa-onnx-whisper-tiny"
      STT_SHA="c46116994e539aa165266d96b325252728429c12535eb9d8b6a2b10f129e66b1" ;;
    whisper-base) STT="sherpa-onnx-whisper-base"
      STT_SHA="911b2083efd7c0dca2ac3b358b75222660dc09fb716d64fbfc417ba6c99ff3de" ;;
    whisper-small) STT="sherpa-onnx-whisper-small"
      STT_SHA="486a46afbb7ba798507190ffe02fea2dd726049af212e774537efac6afb210a6" ;;
    qwen3-asr-gpu) QWEN=1; STT="" ;;  # no sherpa archive: qwen_install sets up its venv and model
    *) echo "config.toml: [stt] engine must be one of nemotron, nemotron-multilingual, parakeet, whisper-tiny, whisper-base, whisper-small, qwen3-asr-gpu (got '$engine')" >&2; exit 1 ;;
  esac
  STT_URL="$ASR_MODELS/$STT.tar.bz2"
}

# qwen_setup: the checks for qwen3-asr-gpu, before any package step: a supported GPU, then the disk
qwen_setup() {
  local gfx free d="$HOME_DIR/.voice-instinct"
  # node 0 is the CPU (0); 110501 is gfx1151 (Strix Halo), the one AMD chip with a torch that works here
  gfx="$(sed -n 's/^gfx_target_version \([1-9][0-9]*\)$/\1/p' "${VI_KFD_TOPOLOGY:-/sys/class/kfd/kfd/topology}"/nodes/*/properties 2>/dev/null || true)"
  if printf '%s\n' "$gfx" | grep -qx 110501; then
    TORCH_INDEX="https://rocm.nightlies.amd.com/v2/gfx1151/" TORCH_VER="2.10.0+rocm7.13.0a20260513"
  elif command -v nvidia-smi >/dev/null; then
    TORCH_INDEX="" TORCH_VER="2.10.0"  # PyPI's CUDA build
  elif [ -n "$gfx" ]; then
    gfx="$(printf '%s\n' "$gfx" | head -1)"
    echo "qwen3-asr-gpu: your AMD GPU (gfx$((gfx / 10000))$(printf '%x%x' $((gfx / 100 % 100)) $((gfx % 100)))) is not supported yet (supported: AMD gfx1151, or NVIDIA). Pick another [stt] engine in config.toml." >&2; exit 1
  else
    echo "qwen3-asr-gpu needs a supported GPU (AMD gfx1151, or NVIDIA with nvidia-smi); none found. Pick another [stt] engine in config.toml." >&2; exit 1
  fi
  if [ -x "$QWEN_DIR/venv/bin/python" ] && [ -f "$QWEN_MODEL/config.json" ]; then return; fi
  [ -e "$d" ] || d="$HOME_DIR"
  free="${VI_FREE_GB:-$(df -Pk "$d" 2>/dev/null | awk 'NR==2 {print int($4 / 1048576)}' || true)}"  # tests override
  case "$free" in ''|*[!0-9]*) echo "qwen3-asr-gpu: could not read the free disk space under $d (got '$free'); it needs at least 12 GB." >&2; exit 1 ;; esac
  if [ "$free" -lt 12 ]; then
    echo "qwen3-asr-gpu needs at least 12 GB free under $HOME_DIR/.voice-instinct (about 10 GB of PyTorch and model); $free GB is free." >&2; exit 1
  fi
}

# qwen_install: the venv (torch installed last, from the GPU's index, so nothing swaps it), the check, the model
qwen_install() {
  local py="$QWEN_DIR/venv/bin/python" idx=()
  [ -x "$py" ] || run "$UV" venv -p 3.12 "$QWEN_DIR/venv"  # AMD's wheels are cp310 to cp312 only
  run "$UV" pip install --python "$py" transformers==5.22.1 soundfile numpy  # nothing that depends on torch: it would pull PyPI's
  [ -n "$TORCH_INDEX" ] && idx=(--index-url "$TORCH_INDEX")
  run "$UV" pip install --python "$py" "${idx[@]}" "torch==$TORCH_VER" "torchaudio==$TORCH_VER" \
    || { echo "Could not install torch $TORCH_VER${TORCH_INDEX:+ from $TORCH_INDEX}; not falling back to another build." >&2; exit 1; }
  # without HSA_OVERRIDE_GFX_VERSION (a workaround some set for older ROCm), as the server was measured; the unit unsets it too
  run env -u HSA_OVERRIDE_GFX_VERSION "$py" -c "import torch; assert torch.version.hip or torch.version.cuda, 'torch ' + torch.__version__ + ' is not a GPU build'; assert torch.cuda.is_available(), 'torch cannot see the GPU'" \
    || { echo "The Qwen engine's PyTorch cannot use the GPU (see above). Pick another [stt] engine in config.toml." >&2; exit 1; }
  if [ -f "$QWEN_MODEL/config.json" ]; then say "keep speech model Qwen3-ASR-1.7B-hf"; return; fi
  # into .part, renamed when complete: a re-run resumes, and the model dir only exists once it is whole
  run "$py" -c "import sys; from huggingface_hub import snapshot_download; snapshot_download('Qwen/Qwen3-ASR-1.7B-hf', revision=sys.argv[1], local_dir=sys.argv[2])" "$QWEN_REV" "$QWEN_MODEL.part"
  run rm -rf "$QWEN_MODEL"  # a leftover dir without config.json would swallow the mv
  run mv "$QWEN_MODEL.part" "$QWEN_MODEL"
}

append_env() { # append_env <file> <key> <value>
  if [ -s "$1" ] && [ -n "$(tail -c1 "$1")" ]; then printf '\n' >>"$1"; fi
  (umask 077; printf '%s=%s\n' "$2" "$3" >>"$1"); chmod 600 "$1"
}

config_and_key() {
  if [ -f "$REPO/config.toml" ]; then say "keep existing config.toml"; else
    say "create config.toml from config.example.toml"; run cp "$REPO/config.example.toml" "$REPO/config.toml"; fi
  local key; key="$(cfgval api_key_env)"
  if [ -n "$key" ] && ! grep -qs "^$key=" "$REPO/.env"; then
    if [ "$DRY" = 1 ] || [ ! -t 0 ]; then say "add $key=... to $REPO/.env (needed for the front model)"; else
      read -r -s -p "$key (input hidden; leave empty to add it later): " val; echo
      if [ -n "$val" ]; then append_env "$REPO/.env" "$key" "$val"; fi
    fi
  fi
}

sway_rule() {
  local size w="" rule="floating enable, sticky enable, border none"
  size="$(cfgval size)"; size="${size:-128}"
  if command -v swaymsg >/dev/null; then
    w="$(swaymsg -t get_outputs 2>/dev/null | python3 -c 'import json,sys; print(next(o["rect"]["width"] for o in json.load(sys.stdin) if o.get("focused")))' 2>/dev/null || true)"
  fi
  if [ -n "$w" ]; then
    rule="$rule, resize set $size $size, move position $((w - size - 24)) 12"
    say "Sway ignores app positioning; add to your config:  for_window [app_id=\"voice-instinct\"] $rule"
  else
    say "swaymsg unavailable, so no resize/move; add to your config:  for_window [app_id=\"voice-instinct\"] $rule"
  fi
  say "         and a key:        bindsym \$mod+n exec $LAUNCHER toggle"
  say "         and, unless your config runs dex --autostart:  exec $LAUNCHER"
  say "         (details: docs/sway.md)"
}

services() {
  local uvbin="$UV" CLAUDE_BIN
  CLAUDE_BIN="$(command -v claude || true)"  # the service's PATH may not reach it (npm-global, nvm)
  local qwen_deps=""  # the backend unit waits for the Qwen service; other engines' units stay as they were
  [ -n "$QWEN" ] && qwen_deps=$'\nWants=voice-instinct-qwen.service\nAfter=voice-instinct-qwen.service'
  write "$UNIT" 644 <<EOF
[Unit]
Description=voice-instinct backend${qwen_deps}

[Service]
WorkingDirectory=$REPO/backend
ExecStart=$uvbin run --project $REPO/backend python bot.py
Restart=on-failure
RestartSec=3
# uv exits 143 when systemd stops it with SIGTERM; that is a normal stop
SuccessExitStatus=143
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin${CLAUDE_BIN:+:${CLAUDE_BIN%/*}}
${CLAUDE_BIN:+Environment=VI_CLAUDE=$CLAUDE_BIN}

[Install]
WantedBy=default.target
EOF
  write "$MEMORY_UNIT" 644 <<EOF
[Unit]
Description=voice-instinct nightly memory run (files the day's conversations into memory)

[Service]
Type=oneshot
WorkingDirectory=$REPO/backend
ExecStart=$uvbin run --project $REPO/backend python consolidate.py
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$HOME_DIR/.local/bin:/usr/local/bin:/usr/bin:/bin${CLAUDE_BIN:+:${CLAUDE_BIN%/*}}
${CLAUDE_BIN:+Environment=VI_CLAUDE=$CLAUDE_BIN}
EOF
  write "$MEMORY_TIMER" 644 <<EOF
[Unit]
Description=voice-instinct nightly memory run

[Timer]
OnCalendar=*-*-* 03:00
# a night the laptop slept through runs at the next boot
Persistent=true

[Install]
WantedBy=timers.target
EOF
  if [ -n "$QWEN" ]; then
    write "$QWEN_UNIT" 644 <<EOF
[Unit]
Description=voice-instinct speech-to-text on the GPU (Qwen3-ASR-1.7B, 127.0.0.1:7861)
StartLimitIntervalSec=300
StartLimitBurst=5

[Service]
ExecStart=$QWEN_DIR/venv/bin/python $REPO/backend/qwen_asr_server.py $QWEN_MODEL
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1
UnsetEnvironment=HSA_OVERRIDE_GFX_VERSION

[Install]
WantedBy=default.target
EOF
  fi
  write "$LAUNCHER" 755 <<EOF
#!/usr/bin/env bash
# voice-instinct launcher: "voice-instinct" starts the orb, "voice-instinct toggle" shows or hides it,
# "voice-instinct quit" quits it (for desktops without a tray).
args=()
[ "\${1:-}" = toggle ] && args=(--toggle)
[ "\${1:-}" = quit ] && args=(--quit)
# The real binary: node_modules/.bin/electron is a node script, and many desktops have no system node.
exec "$REPO/frontend/node_modules/electron/dist/electron" "$REPO/frontend" "\${args[@]}"
EOF
  write "$AUTOSTART" 644 <<EOF
[Desktop Entry]
Type=Application
Name=voice-instinct
Exec=$LAUNCHER
X-GNOME-Autostart-enabled=true
EOF
  if [ "$DRY" = 1 ] || systemctl --user show-environment >/dev/null 2>&1; then
    if [ -z "$QWEN" ] && [ -f "$QWEN_UNIT" ]; then  # switched away from qwen3-asr-gpu; removed before the reload
      run systemctl --user disable --now voice-instinct-qwen.service
      run rm -f "$QWEN_UNIT"
    fi
    run systemctl --user daemon-reload
    if [ -n "$QWEN" ]; then
      run systemctl --user enable voice-instinct-qwen.service
      run systemctl --user restart voice-instinct-qwen.service  # about a minute of warm-up before it answers
    fi
    run systemctl --user enable voice-instinct.service
    run systemctl --user restart voice-instinct.service  # re-run = update: pick up new code
    run systemctl --user enable --now voice-instinct-memory.timer
  else
    say "No systemd user session here; start the backend with: cd $REPO/backend && uv run bot.py"
  fi
  case "${XDG_CURRENT_DESKTOP:-}${SWAYSOCK:+sway}${HYPRLAND_INSTANCE_SIGNATURE:+hyprland}" in
    *sway*) sway_rule ;;
    *GNOME*) say "GNOME shows the tray icon (status, mute, quit) only with the AppIndicator extension (gnome-shell-extension-appindicator); without it, quit with: $LAUNCHER quit" ;;
    *hyprland*|*Hyprland*) say "Hyprland: windowrulev2 = float, class:^(voice-instinct)\$ and windowrulev2 = pin, class:^(voice-instinct)\$, and exec-once = $LAUNCHER (Hyprland does not run XDG autostart)" ;;
  esac
}

uninstall() {
  # idempotent: a missing unit or no user systemd session must not stop the cleanup
  if [ -x "$LAUNCHER" ] || [ "$DRY" = 1 ]; then  # the orb would keep running until logout
    if [ "$DRY" = 1 ]; then run "$LAUNCHER" quit; else timeout 5 "$LAUNCHER" quit >/dev/null 2>&1 || true; fi
  fi
  if [ "$DRY" = 1 ]; then run systemctl --user disable --now voice-instinct.service voice-instinct-memory.timer
  else systemctl --user disable --now voice-instinct.service voice-instinct-memory.timer 2>/dev/null || true; fi
  if [ -f "$QWEN_UNIT" ]; then  # its own call: disabling a unit that doesn't exist fails the whole call
    if [ "$DRY" = 1 ]; then run systemctl --user disable --now voice-instinct-qwen.service
    else systemctl --user disable --now voice-instinct-qwen.service 2>/dev/null || true; fi
  fi
  run rm -f "$UNIT" "$MEMORY_UNIT" "$MEMORY_TIMER" "$QWEN_UNIT" "$AUTOSTART" "$LAUNCHER"
  if [ "$DRY" = 1 ]; then run systemctl --user daemon-reload
  else systemctl --user daemon-reload 2>/dev/null || true; fi
  if [ "$PURGE" = 1 ]; then say "remove data in $HOME_DIR/.voice-instinct (models, database, workspace)"; run rm -rf "$HOME_DIR/.voice-instinct"; fi
  local qwen=""; [ -d "$QWEN_DIR" ] && qwen=", plus about 10 GB for the Qwen GPU engine"
  say "voice-instinct removed$([ "$PURGE" = 1 ] || echo "; ~/.voice-instinct is kept: your data, plus about 2 GB of models, Node and the errand browser$qwen (--purge deletes it)")"
}

if [ "$UNINSTALL" = 1 ]; then uninstall; exit 0; fi

if [ -z "$NODE_ARCH" ]; then echo "unsupported CPU: $(uname -m) (supported: x86_64, aarch64)" >&2; exit 1; fi
stt_choice  # an unknown engine stops here, before any package or download step
if [ -n "$QWEN" ]; then qwen_setup; fi  # so does qwen3-asr-gpu without a supported GPU or the disk space
distro_install
node_install
command -v uv >/dev/null || run sh -c "curl -LsSf https://astral.sh/uv/install.sh | sh"
UV="$(command -v uv || echo "$HOME_DIR/.local/bin/uv")"  # the fresh install is not on PATH yet
models
if [ -n "$QWEN" ]; then qwen_install
elif [ -d "$QWEN_DIR" ] || [ -d "$QWEN_MODEL" ] || [ -d "$QWEN_MODEL.part" ]; then
  say "The Qwen GPU engine is no longer used; to free about 10 GB: rm -rf $QWEN_DIR $QWEN_MODEL $QWEN_MODEL.part"
fi
config_and_key
run sh -c "cd '$REPO/backend' && $UV sync --python 3.13"
# npm 12 blocks install scripts, so fetch the Electron binary explicitly (harmless where npm already did)
run sh -c "export PATH=\"$NODE_DIR/bin:\$PATH\"; cd '$REPO/frontend' && npm ci && node node_modules/electron/install.js && npm run build"
# The errand browser: Playwright MCP (pinned; npm checks the registry's integrity hashes) and its Chromium.
# The agent gets a fresh in-memory profile each run, never signed in to anything (backend/autonomy.py).
BROWSER_DIR="$HOME_DIR/.voice-instinct/browser" BROWSER_MCP=0.0.83
if grep -qs "\"version\": \"$BROWSER_MCP\"" "$BROWSER_DIR/node_modules/@playwright/mcp/package.json" && compgen -G "$BROWSER_DIR/browsers/chromium*" >/dev/null; then
  say "keep errand browser (Playwright MCP $BROWSER_MCP)"
else
  # Chromium inside $BROWSER_DIR (not the shared ~/.cache/ms-playwright): --purge removes it all.
  run sh -c "export PATH=\"$NODE_DIR/bin:\$PATH\" PLAYWRIGHT_BROWSERS_PATH='$BROWSER_DIR/browsers'; npm install --prefix '$BROWSER_DIR' --no-audit --no-fund @playwright/mcp@$BROWSER_MCP && node '$BROWSER_DIR/node_modules/playwright/cli.js' install chromium"
fi
# Ubuntu 24.04+ (AppArmor) blocks unprivileged user namespaces; Electron then needs its SUID sandbox helper.
if [ "$(cat "${VI_USERNS_FILE:-/proc/sys/kernel/apparmor_restrict_unprivileged_userns}" 2>/dev/null)" = 1 ]; then
  helper="$REPO/frontend/node_modules/electron/dist/chrome-sandbox"
  say "Your system restricts user namespaces, so Electron's sandbox helper must be owned by root (sudo):"
  run sudo chown root:root "$helper"
  run sudo chmod 4755 "$helper"
fi
if ! command -v claude >/dev/null; then
  say "Install Claude Code (https://code.claude.com/docs) and log in with 'claude', then re-run this script."
fi
services
name="$(cfgval name)"
if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
  # the orb, now; from the next login it starts by itself
  if [ "$DRY" = 1 ]; then run "$LAUNCHER" quit; run setsid -f "$LAUNCHER"  # quit first: an update must run the new code
  else
    timeout 5 "$LAUNCHER" quit >/dev/null 2>&1 || true
    # wait for the old orb to let go of its single-instance lock, or the new one exits straight away
    for _ in 1 2 3 4 5 6 7 8 9 10; do pgrep -f "electron/dist/electron.*$REPO/frontend" >/dev/null || break; sleep 0.5; done
    setsid -f "$LAUNCHER" >/dev/null 2>&1
  fi
  say "Done. Say \"${name:-Nova}\"."
else
  say "Done. Start the orb with $LAUNCHER (it starts by itself from your next login), then say \"${name:-Nova}\"."
fi
case "$(cfgval engine)" in parakeet|whisper-*|qwen3-asr-gpu)  # the voice check needs Parakeet, Whisper or Qwen (#32)
  say "To make only your voice wake it (one minute, with the orb open): cd $REPO/backend && uv run enroll.py" ;;
esac
