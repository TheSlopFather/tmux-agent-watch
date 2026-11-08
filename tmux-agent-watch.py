#!/usr/bin/env python3
"""tmux agent-watch v5: base-only watcher with diagnostic SSH probing.

Install ONLY on the outer tmux host:
    python3 tmux-agent-watch-installer-v5.py
Uninstall:
    python3 tmux-agent-watch-installer-v5.py --uninstall

Local Claude Code uses optional native event hooks for reliable state changes.
Remote tmux instances can be queried over noninteractive SSH, including
hidden windows, without installing software on the remote host.
Requires Python 3, tmux, and SSH on the base host.
"""
import argparse
import json
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import shlex
import subprocess
import sys
import time
from datetime import datetime

HOME = Path.home()
BIN = HOME / '.local/bin/tmux-agent-watch'
STATE_DIR = HOME / '.local/state/tmux-agent-watch'
CLAUDE_SETTINGS = HOME / '.claude/settings.json'
CLAUDE_HOOK_MARKER = '.local/bin/tmux-agent-watch" event '
MARK_START = '# >>> tmux-agent-watch base-only >>>'
MARK_END = '# <<< tmux-agent-watch base-only <<<' 
WINDOW_FLAG = '#{?#{==:#{@agentwatch_state},wait}, ✋,#{?#{==:#{@agentwatch_state},work}, 👨‍💻,#{?#{==:#{@agentwatch_state},ready}, 🧑‍🔧,}}}'
INTERVAL = 2.0
REMOTE_INTERVAL = 8.0
REMOTE_TIMEOUT = 12.0
# Keep serving the last successful remote probe for this long when a probe
# fails/times out, so a hidden-window agent's emoji does not flicker on a
# single dropped SSH poll. A genuinely unreachable host clears after this.
REMOTE_GRACE = 30.0
REMOTE_LINKS = HOME / '.config/tmux-agent-watch/remotes.json'
# Convention: a dedicated, passphrase-less key named ~/.ssh/agentwatch is
# offered for every read-only probe when present. This lets an unattended
# watcher authenticate one hop down (e.g. a jump box → a fleet host) without
# an ssh-agent or per-host ssh_config, so deep nesting "just works": drop an
# authorized ~/.ssh/agentwatch on each tier that runs the watcher.
AGENTWATCH_KEY = HOME / '.ssh' / 'agentwatch'

# Urgency ranking used everywhere states are combined (per-pane, per-window,
# and when rolling up nested/remote tmux tiers). A tab inherits the MOST urgent
# state of any agent beneath it: wait (needs a human) > ready (idle, awaiting a
# prompt) > work (busy, leave it alone) > none.
PRIORITY = {'': 0, 'work': 1, 'ready': 2, 'wait': 3}

# Read-only remote inventory. The login tmux server is not necessarily the
# default socket: users commonly run tmux -L or -S. Search own UID's tmux
# sockets and report an explicit error if none can be inspected.
REMOTE_COLLECT = r''' 
# This script is streamed over SSH: nothing is installed or written remotely.
TMUX_BIN="$(command -v tmux 2>/dev/null)"
if [ -z "$TMUX_BIN" ]; then
  for candidate in /usr/bin/tmux /usr/local/bin/tmux "$HOME/.local/bin/tmux"; do
    if [ -x "$candidate" ]; then TMUX_BIN="$candidate"; break; fi
  done
fi
if [ -z "$TMUX_BIN" ]; then echo 'AWERROR tmux executable not found in remote PATH' >&2; exit 3; fi
# Find the agent process even if the window is named zsh or its UI footer
# disappears while the window is hidden. Export only the agent label, never
# arbitrary command-line arguments from the remote host.
agent_under_root() {
  root="$1"
  case "$root" in ''|*[!0-9]*) return ;; esac
  command -v ps >/dev/null 2>&1 || return
  command -v awk >/dev/null 2>&1 || return
  ps -eo pid=,ppid=,args= 2>/dev/null | awk -v root="$root" '
  {
    pid=$1; parent[pid]=$2
    line=$0
    sub(/^[[:space:]]*[0-9]+[[:space:]]+[0-9]+[[:space:]]+/, "", line)
    command_line[pid]=tolower(line)
  }
  END {
    for (pid in parent) {
      a=command_line[pid]; label=""
      if (a ~ /(^|[\/[:space:]])claude([\/[:space:]._-]|$)/ || a ~ /@anthropic-ai\/claude-code/) label="claude"
      else if (a ~ /(^|[\/[:space:]])hermes([\/[:space:]._-]|$)/ || a ~ /hermes-agent/) label="hermes"
      else if (a ~ /(^|[\/[:space:]])codex([\/[:space:]._-]|$)/) label="codex"
      else if (a ~ /(^|[\/[:space:]])gemini([\/[:space:]._-]|$)/ || a ~ /gemini-cli/) label="gemini"
      else if (a ~ /(^|[\/[:space:]])opencode([\/[:space:]._-]|$)/) label="opencode"
      else if (a ~ /(^|[\/[:space:]])aider([\/[:space:]._-]|$)/) label="aider"
      if (!label) continue
      current=pid
      for (depth=0; depth<40 && current>1; depth++) {
        if (current == root) { print label; exit }
        if (!(current in parent)) break
        current=parent[current]
      }
    }
  }'
}
# Always enumerate *all* windows, never rely on selected/active client.
emit_panes() {
  sock="$1"
  if [ "$sock" = default ]; then
    inventory="$("$TMUX_BIN" list-panes -a -F '#{pane_id}@@AWF@@#{session_name}@@AWF@@#{window_name}@@AWF@@#{pane_current_command}@@AWF@@#{pane_title}@@AWF@@#{window_active}@@AWF@@#{pane_pid}' 2>/dev/null)" || return 1
  else
    inventory="$("$TMUX_BIN" -S "$sock" list-panes -a -F '#{pane_id}@@AWF@@#{session_name}@@AWF@@#{window_name}@@AWF@@#{pane_current_command}@@AWF@@#{pane_title}@@AWF@@#{window_active}@@AWF@@#{pane_pid}' 2>/dev/null)" || return 1
  fi
  [ -n "$inventory" ] || return 1
  printf '\036AWSOCK\t%s\n' "$sock"
  # Split on a PRINTABLE sentinel, never a raw control byte: tmux < 3.6
  # (e.g. 3.5a) octal-escapes non-printable bytes in -F output (\034),
  # which breaks IFS field splitting on the remote host.
  printf '%s\n' "$inventory" | while IFS= read -r line; do
    rest=$line
    pane=${rest%%@@AWF@@*};   rest=${rest#*@@AWF@@}
    sess=${rest%%@@AWF@@*};   rest=${rest#*@@AWF@@}
    name=${rest%%@@AWF@@*};   rest=${rest#*@@AWF@@}
    cmd=${rest%%@@AWF@@*};    rest=${rest#*@@AWF@@}
    title=${rest%%@@AWF@@*};  rest=${rest#*@@AWF@@}
    active=${rest%%@@AWF@@*}; rest=${rest#*@@AWF@@}
    pid=$rest
    case "$pane" in %*) ;; *) continue ;; esac
    agent="$(agent_under_root "$pid" | head -n 1)"
    printf '\036AWP\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$pane" "$sess" "$name" "$cmd" "$title" "$active" "$agent"
    if [ "$sock" = default ]; then
      "$TMUX_BIN" capture-pane -p -J -t "$pane" 2>/dev/null ||
        "$TMUX_BIN" capture-pane -p -t "$pane" 2>/dev/null ||
        "$TMUX_BIN" capture-pane -a -p -t "$pane" 2>/dev/null
    else
      "$TMUX_BIN" -S "$sock" capture-pane -p -J -t "$pane" 2>/dev/null ||
        "$TMUX_BIN" -S "$sock" capture-pane -p -t "$pane" 2>/dev/null ||
        "$TMUX_BIN" -S "$sock" capture-pane -a -p -t "$pane" 2>/dev/null
    fi | tail -n 90
    printf '\036AWE\n'
  done
}
printf '\036AWBEGIN\n'
found=0
if [ -n "${1:-}" ]; then
  sock="$1"
  if [ ! -S "$sock" ]; then echo "AWERROR explicit tmux socket not found: $sock" >&2; exit 4; fi
  emit_panes "$sock" || { echo "AWERROR cannot query tmux socket: $sock" >&2; exit 4; }
  found=1
else
  for directory in "${TMUX_TMPDIR:-/tmp}/tmux-$(id -u)" "/tmp/tmux-$(id -u)"; do
    [ -d "$directory" ] || continue
    for sock in "$directory"/*; do
      [ -S "$sock" ] || continue
      # Avoid querying the same socket twice when TMUX_TMPDIR points at /tmp.
      case " $seen " in *" $sock "*) continue ;; esac
      seen="$seen $sock"
      if emit_panes "$sock"; then found=$((found+1)); fi
    done
  done
  if [ "$found" -eq 0 ]; then
    if emit_panes default; then found=1; fi
  fi
fi
printf '\036AWEND\n'
if [ "$found" -eq 0 ]; then echo 'AWERROR no accessible tmux server sockets for remote user' >&2; exit 4; fi
''' 

# Signals are intentionally conservative: explicit selection dialogs are much
# more reliable than a bare idle terminal cursor.
CHOICES = re.compile(r'(?im)^\s*(?:[❯›>➜]\s*)?(?:[1-9][.)]\s*)?(?:yes(?:,|\b)|no(?:,|\b)|allow\b|deny\b|reject\b|approve\b|once\b|always\b)')
EXPLICIT = re.compile(
    r'(?i)(?:do you want to proceed\?|do you want to make this edit\?|'
    r'do you want to (?:allow|run|approve|execute)\b|permission (?:request|required|needed)|'
    r'(?:approve|allow|deny) (?:this|the) (?:tool|command|action|request)|'
    r'waiting for (?:your )?(?:approval|confirmation|permission)|'
    r'approval (?:required|requested)|allow once|allow always|always allow|'
    r'allow for this session|esc to cancel.{0,35}tab to amend|'
    r'would you like to (?:allow|approve|run|proceed)|'
    r'press (?:enter|return) to (?:confirm|accept|approve))'
)
DECISION_FOOTER = re.compile(r'(?i)(?:esc to cancel|tab to amend|enter to select|arrow keys|accept.{0,30}reject|allow.{0,30}deny)')
NUMBERED = re.compile(r'(?m)^\s*[❯›>]?\s*[1-9][.)]\s+\S+')
AGENT_NAMES = re.compile(r'(?i)(?:\bhermes\b|\bclaude\b|\bcodex\b|\bgemini\b|\bopencode\b|\bopen.?code\b|\baider\b|\b(?:copilot|cursor)\b)')
AGENT_UI = re.compile(r'(?i)(?:hermes agent|hermes cli|claude code|openai codex|gemini cli|opencode|context left|bypass permissions|accept edits|shift\+tab to cycle)')
# Screen changes are NEVER proof of agent work. Typing also changes a screen.
# The remote fallback recognizes only explicit live UI signals, not activity
# inferred from terminal redraws or a session's scrollback.
BUSY = re.compile(
    r'(?im)(?:esc to interrupt|esc to stop|ctrl\+c to interrupt|'
    r'^(?:[ ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✢✳✶✻*·]+)?\s*(?:thinking|working|running tool|'
    r'pondering|contemplating)\s*(?:[.…\-]|\(|$)|'
    r'\b(?:thinking|working)\s+[0-9]+s\b)'
)
# Only match real agent prompt characters here, never the generic shell '>'.
COMPOSER = re.compile(r'^\s*[❯›]\s*(?:.*)$')
HERMES_COMPOSER = re.compile(r'(?i)^\s*(?:you|message|prompt)\s*[>❯:]\s*(?:.*)$')
# Hermes draws a live activity bar like "── ⠋ Working… · 2m · ↓ 8.0k tok ──".
# A spinner glyph immediately before a word + ellipsis is a current-turn
# signal that does not occur in a quiescent pane's scrollback prose.
HERMES_SPINNER = '⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷✢✳✶✻✽'
HERMES_WORKING = re.compile('[' + HERMES_SPINNER + r']\s*[A-Za-z]+\s*(?:…|\.\.\.)')
# A visible interrupt/stop affordance is the strongest live-turn signal and is
# never inferred from screen redraws. Covers Claude's "esc to interrupt" and
# Hermes' dialect ("enter interrupt", "ctrl+c stop").
INTERRUPT = re.compile(r'(?i)(?:esc|enter|ctrl[\+-]c)\s+(?:to\s+)?(?:interrupt|stop)\b')


def tmux_args(socket=None):
    return ['tmux'] + (['-S', socket] if socket else [])


def tmux_call(socket, *args, timeout=4):
    try:
        return subprocess.run(tmux_args(socket) + list(args), capture_output=True,
                              text=True, encoding='utf-8', errors='replace', timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return subprocess.CompletedProcess([], 1, '', '')


def socket_guess():
    # TMUX is /path/to/socket,pid,session when invoked from the OUTER session.
    inherited = os.environ.get('TMUX', '')
    if inherited and ',' in inherited:
        sock = inherited.split(',', 1)[0]
        if sock:
            return sock
    r = tmux_call(None, 'display-message', '-p', '#{socket_path}')
    return r.stdout.strip() if r.returncode == 0 else None


def state_file(socket):
    tag = hashlib.sha256(socket.encode('utf-8')).hexdigest()[:16]
    return STATE_DIR / (tag + '.pid')


def pid_is_ours(pid):
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    proc = Path('/proc') / str(pid) / 'cmdline'
    if proc.exists():
        try:
            cmd = proc.read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
            return 'tmux-agent-watch' in cmd or 'install-tmux-agent-indicator-' in cmd
        except OSError:
            return False
    return True  # macOS/BSD: PID was taken from our private pid file.


def running_pid(socket):
    try:
        pid = int(state_file(socket).read_text().strip())
        return pid if pid_is_ours(pid) else None
    except (OSError, ValueError):
        return None


def set_state(socket, window, state):
    if state:
        tmux_call(socket, 'set-option', '-w', '-t', window, '@agentwatch_state', state)
    else:
        tmux_call(socket, 'set-option', '-wu', '-t', window, '@agentwatch_state')


def list_panes(socket):
    r = tmux_call(socket, 'list-panes', '-a', '-F', '#{pane_id}\t#{window_id}\t#{window_name}\t#{pane_current_command}')
    if r.returncode != 0:
        return None
    panes = []
    seen = set()
    for line in r.stdout.splitlines():
        parts = line.split('\t', 3)
        if len(parts) != 4:
            continue
        pane, window, name, command = parts
        if pane not in seen:
            seen.add(pane)
            panes.append((pane, window, name, command))
    return panes


def classify(screen, name='', command=''):
    """Return (state, reason), based on visible, current UI cues only."""
    lines = [line.rstrip() for line in screen.splitlines() if line.strip()]
    if not lines:
        return '', 'empty'
    focus_lines = lines[-12:]
    focus = '\n'.join(focus_lines)
    identity = name + '\n' + command + '\n' + '\n'.join(lines[:5] + lines[-8:])
    agent = bool(AGENT_NAMES.search(identity) or AGENT_UI.search(identity))
    explicit = bool(EXPLICIT.search(focus))
    choices = len(CHOICES.findall(focus))
    hints = bool(DECISION_FOOTER.search(focus))
    if explicit and (choices >= 2 or hints or agent):
        return 'wait', 'approval/choice prompt'
    if agent and hints and (choices >= 2 or len(NUMBERED.findall(focus)) >= 2):
        return 'wait', 'interactive choice'

    # The composer may contain *typed text*. It remains idle until submitted;
    # never label it work because the screen or cursor changes.
    tail_lines = lines[-6:]
    composer = (any(COMPOSER.match(line) for line in tail_lines)
                or (agent and any(HERMES_COMPOSER.match(line) for line in tail_lines)))
    # A bare ASCII '>' is too generic for local screen detection; it is safe
    # only when the remote process tree confirms a live agent beneath the pane.
    if 'agentproc:' in command.lower() and agent:
        composer = composer or any(re.match(r'^\s*>\s*(?:.*)$', line) for line in tail_lines)


    # Recognize only current (bottom-of-screen) agent activity. In particular,
    # a historical 'Thinking...' line in scrollback must not override a prompt.
    recent = '\n'.join(lines[-4:])
    # Hermes publishes its state in a footer activity bar. Evaluate live-turn
    # (busy) signals BEFORE any composer, because Hermes keeps its composer
    # ("❯ type to redirect this turn") visible on screen while still working.
    hermes = bool(re.search(r'(?i)\bhermes\b', name + ' ' + command))
    # Footer region, widened to span the whole Hermes status block, with the
    # composer line(s) removed so typed/queued text cannot mask the bar.
    hermes_status = '\n'.join(l for l in lines[-6:]
                              if not COMPOSER.match(l)
                              and not HERMES_COMPOSER.match(l))
    if agent and INTERRUPT.search(recent):
        return 'work', 'visible interrupt affordance'
    if hermes and HERMES_WORKING.search(hermes_status):
        return 'work', 'Hermes TUI activity bar'
    # Fallback for spinner frames outside the known set: a gerund + ellipsis
    # followed by the bar's middot/pipe separator or end of line (not prose).
    if hermes and re.search(r'(?im)\b(?:thinking|working|running|generating|'
                            r'reflecting|planning|pondering|contemplating|searching|'
                            r'reading|writing|compiling|building)\b\s*(?:…|\.{2,})'
                            r'\s*(?:·|\||$)', hermes_status):
        return 'work', 'Hermes TUI busy status'
    if hermes and re.search(r'(?i)\b(?:pondering|contemplating)\b.{0,26}\(\d+(?:\.\d+)?s\)', hermes_status):
        return 'work', 'Hermes classic thinking animation'
    # Idle/ready: an explicit "ready" in the bar (possibly prefixed by
    # box-drawing/spinner/sigil glyphs) or the original bare form.
    if hermes and (re.search(r'(?im)^\W*ready\b', hermes_status)
                   or re.search(r'(?im)^\s*ready\s*(?:[·|].*)?$', hermes_status)):
        return 'ready', 'Hermes TUI ready status'
    if composer and (agent or any('shift+tab to cycle' in l.lower() for l in tail_lines)):
        return 'ready', 'visible agent input composer'
    if agent and BUSY.search(recent):
        return 'work', 'visible agent busy indicator'
    return '', 'no current agent state detected'


def native_state(socket, pane):
    r = tmux_call(socket, 'show-option', '-p', '-qv', '-t', pane,
                  '@agentwatch_native_state')
    value = r.stdout.strip() if r.returncode == 0 else ''
    return value if value in ('ready', 'work', 'wait') else ''


def saved_remote_links():
    try:
        data = json.loads(REMOTE_LINKS.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_remote_links(links):
    REMOTE_LINKS.parent.mkdir(parents=True, exist_ok=True)
    tmp = REMOTE_LINKS.with_suffix('.tmp')
    tmp.write_text(json.dumps(links, indent=2) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)
    tmp.replace(REMOTE_LINKS)


def pane_process_ids(socket):
    r = tmux_call(socket, 'list-panes', '-a', '-F', '#{pane_id}\t#{pane_pid}')
    ids = {}
    if r.returncode == 0:
        for line in r.stdout.splitlines():
            chunks = line.split('\t')
            if len(chunks) == 2 and chunks[1].isdigit():
                ids[chunks[0]] = int(chunks[1])
    return ids


def process_args(pid):
    try:
        raw = Path(f'/proc/{pid}/cmdline').read_bytes()
        return [x.decode('utf-8', 'replace') for x in raw.strip(b'\0').split(b'\0') if x]
    except OSError:
        return []


def child_pids(pid):
    try:
        return [int(x) for x in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
    except (OSError, ValueError):
        return []


def ppid_of(pid):
    try:
        stat = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8')
        return int(stat.rsplit(')', 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return 0


def ancestry(pid=None, max_depth=40):
    pid = pid or os.getpid()
    result = []
    while pid > 1 and len(result) < max_depth and pid not in result:
        result.append(pid)
        pid = ppid_of(pid)
    return result


def ancestor_tmux_env(lineage):
    """Find TMUX/TMUX_PANE inherited by a shell/agent when hook env lacks it."""
    result = {}
    for pid in lineage:
        try:
            values = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
        except OSError:
            continue
        for item in values:
            for var in ('TMUX', 'TMUX_PANE'):
                if item.startswith((var+'=').encode()) and var not in result:
                    result[var] = item.partition(b'=')[2].decode('utf-8', 'replace')
        if len(result) == 2:
            break
    return result


def local_agent_descendant(pid):
    """Discover local agent children even if the tmux window is named shell."""
    if not pid:
        return ''
    queue = [(pid, 0)]
    seen = set()
    while queue and len(seen) < 150:
        current, depth = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        args = process_args(current)
        if args:
            exe = Path(args[0]).name.lower()
            if exe in ('claude', 'codex', 'hermes', 'gemini', 'opencode', 'aider'):
                return exe
            if 'claude-code' in exe or (exe in ('node', 'bun') and
                    any('claude-code' in a or '/claude/cli.js' in a for a in args[1:])):
                return 'claude'
        if depth < 5:
            queue.extend((p, depth+1) for p in child_pids(current))
    return ''


def local_tmux_sockets():
    result = []
    for folder in (Path(os.environ.get('TMUX_TMPDIR', '/tmp')) / f'tmux-{os.getuid()}',
                   Path('/tmp') / f'tmux-{os.getuid()}'):
        if not folder.is_dir():
            continue
        for sock in folder.iterdir():
            try:
                if sock.is_socket() and str(sock) not in result:
                    result.append(str(sock))
            except OSError:
                pass
    return result


def find_hook_pane():
    """Resolve current pane even if Claude sanitizes TMUX_PANE (known bug)."""
    lineage = ancestry()
    env = {**ancestor_tmux_env(lineage), **{k: v for k, v in os.environ.items()
                                                if k in ('TMUX', 'TMUX_PANE') and v}}
    desired_pane = env.get('TMUX_PANE', '')
    desired_sock = env.get('TMUX', '').split(',', 1)[0]
    candidates = [desired_sock] if desired_sock else []
    candidates.extend(x for x in local_tmux_sockets() if x not in candidates)
    candidates.append(None)  # default server fallback
    matches = []
    for sock in candidates:
        r = tmux_call(sock, 'list-panes', '-a', '-F', '#{pane_id}\t#{pane_pid}')
        if r.returncode != 0:
            continue
        for item in r.stdout.splitlines():
            parts = item.split('\t')
            if len(parts) != 2 or not parts[1].isdigit():
                continue
            pane, root = parts[0], int(parts[1])
            # Prefer a pane whose shell/command is in our actual ancestry;
            # never guess from cwd or select the active pane of another user.
            if root in lineage:
                return sock, pane
            if pane == desired_pane and (not desired_sock or sock == desired_sock):
                matches.append((sock, pane))
    if len(matches) == 1:
        return matches[0]
    return None, None


def parse_ssh_args(args):
    """Extract destination and relevant connection options from a running ssh.

    Does not use a shell or execute remote commands supplied by the process.
    """
    if not args or Path(args[0]).name != 'ssh':
        return None
    opts = []
    host = None
    i = 1
    takes_arg = set('bBcDEeFIiJLlmOopRSWw')
    keep = set('FipJlo')
    while i < len(args):
        arg = args[i]
        if arg == '--':
            i += 1
            break
        if not arg.startswith('-') or arg == '-':
            break
        if len(arg) >= 2 and arg[1] in takes_arg:
            key = arg[1]
            val = arg[2:] if len(arg) > 2 else (args[i + 1] if i + 1 < len(args) else '')
            if not val:
                return None
            if not arg[2:]:
                i += 1
            if key in keep:
                opts.extend(['-' + key, val])
        i += 1
    if i < len(args):
        host = args[i]
    # Avoid options or whitespace becoming host arguments. OpenSSH config
    # aliases, user@hosts, and IPv6 addresses without spaces are supported.
    if not host or not re.fullmatch(r'[\w.\-:@\[\]%]+', host) or host.startswith('-'):
        return None
    return {'host': host, 'opts': opts}


def discovered_ssh(pid):
    """Linux /proc: find ssh running under the tmux pane (not other panes)."""
    queue = [(pid, 0)]
    seen = set()
    while queue and len(seen) < 100:
        current, depth = queue.pop(0)
        if current in seen:
            continue
        seen.add(current)
        result = parse_ssh_args(process_args(current))
        if result:
            return result
        if depth < 5:
            queue.extend((c, depth + 1) for c in child_pids(current))
    return None



def process_descendants(pid, max_nodes=200, max_depth=8):
    """All processes beneath a tmux pane's initial shell (Linux /proc)."""
    if not pid:
        return set()
    seen = set()
    todo = [(pid, 0)]
    while todo and len(seen) < max_nodes:
        current, depth = todo.pop(0)
        if current in seen:
            continue
        seen.add(current)
        if depth < max_depth:
            todo.extend((child, depth + 1) for child in child_pids(current))
    return seen


def local_nested_index(outer_socket):
    """Map attached LOCAL nested tmux clients to their servers.

    Unlike grep of the outer pane's screen, client_pid maps an attached nested
    tmux server back to the process inside the outer tmux pane. This works
    when the nested pane contains an SSH process in a *hidden* window.
    """
    index = {}
    for sock in local_tmux_sockets():
        if sock == outer_socket:
            continue
        clients = tmux_call(sock, 'list-clients', '-F', '#{client_pid}', timeout=2)
        if clients.returncode != 0:
            continue
        client_pids = {int(x) for x in clients.stdout.split() if x.isdigit()}
        if not client_pids:
            continue
        nested_panes = list_panes(sock) or []
        index[sock] = (client_pids, nested_panes, pane_process_ids(sock))
    return index


def linked_nested_servers(root_pid, index, max_levels=3):
    """Resolve local nested tmux clients, including multi-level nested clients."""
    if not root_pid:
        return []
    linked = []
    root_pids = [root_pid]
    seen = set()
    for _ in range(max_levels):
        new_pids = []
        for root in root_pids:
            children = process_descendants(root)
            for sock, (clients, panes, pids) in index.items():
                if sock in seen or not (children & clients):
                    continue
                seen.add(sock)
                linked.append((sock, panes, pids))
                new_pids.extend(pids.values())
        if not new_pids:
            break
        root_pids = new_pids
    return linked


def ssh_agent_for_pid(pid):
    """Reuse the agent socket that an existing interactive SSH process uses."""
    try:
        env = Path(f'/proc/{pid}/environ').read_bytes().split(b'\0')
    except OSError:
        return None
    for item in env:
        if item.startswith(b'SSH_AUTH_SOCK='):
            path = item.split(b'=', 1)[1].decode('utf-8', 'replace')
            try:
                if Path(path).is_socket():
                    return path
            except OSError:
                return None
    return None


def _is_live_socket(path):
    try:
        return bool(path) and Path(path).is_socket()
    except OSError:
        return False


def discover_live_agent_socket(preferred=None):
    """Find a usable ssh-agent socket for a non-interactive probe.

    Prefers an explicit path, then the current environment, then well-known
    runtime locations (gnome-keyring/gcr, systemd, forwarded agents). Returns
    None if nothing live is found, letting ssh fall back to on-disk keys.
    """
    runtime = os.environ.get('XDG_RUNTIME_DIR') or f'/run/user/{os.getuid()}'
    candidates = [preferred, os.environ.get('SSH_AUTH_SOCK'),
                  f'{runtime}/gcr/ssh', f'{runtime}/keyring/ssh',
                  f'{runtime}/ssh-agent.socket']
    try:
        candidates += sorted(str(p) for p in Path('/tmp').glob('ssh-*/agent.*'))
    except OSError:
        pass
    for cand in candidates:
        if _is_live_socket(cand):
            return cand
    return None


def discovered_ssh_targets(pid):
    """Detect all SSH subprocesses under one pane (not just the first)."""
    matches = []
    for child in process_descendants(pid):
        parsed = parse_ssh_args(process_args(child))
        if parsed:
            agent = ssh_agent_for_pid(child)
            if agent:
                parsed['ssh_auth_sock'] = agent
            matches.append(parsed)
    # Uniqueness without dependence on process enumeration order.
    return list({(t['host'], tuple(t.get('opts', [])), t.get('ssh_auth_sock')): t
                 for t in matches}.values())

def parse_remote_records(output, session_filter=None):
    """Parse all remote panes; keep socket identity and foreground metadata."""
    if '\x1eAWBEGIN\n' not in output or '\x1eAWEND\n' not in output:
        return []
    rows = []
    current_socket = 'unknown'
    segments = output.split('\x1e')
    for index, segment in enumerate(segments):
        if segment.startswith('AWSOCK\t'):
            current_socket = segment.split('\n', 1)[0][7:]
        elif segment.startswith('AWP\t'):
            head, sep, rest = segment.partition('\n')
            cells = head[4:].split('\t')
            if not sep or len(cells) < 5 or index + 1 >= len(segments) or not segments[index + 1].startswith('AWE\n'):
                continue
            pane, sess, name, command, title = cells[:5]
            active = cells[5] if len(cells) >= 6 else '1'
            proc_agent = cells[6] if len(cells) >= 7 else ''
            screen = rest
            if pane.startswith('%') and (session_filter is None or sess == session_filter):
                rows.append((pane, sess, name, command, title, screen,
                             current_socket, active, proc_agent))
    return rows


def remote_probe(target):
    """Read-only SSH probe, returning (rows, error, socket_count)."""
    host = target['host']
    opts = target.get('opts', [])
    session = target.get('session')
    remote_shell = 'sh -s'
    if target.get('socket'):
        remote_shell += ' -- ' + shlex.quote(target['socket'])
    # Offer the dedicated agentwatch key (in addition to agent/default keys, so
    # hosts reached via other credentials are unaffected) when it is present.
    key_opts = ['-i', str(AGENTWATCH_KEY)] if AGENTWATCH_KEY.is_file() else []
    cmd = ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectionAttempts=1',
           '-o', 'ConnectTimeout=4', '-o', 'NumberOfPasswordPrompts=0'] + key_opts + opts + [host, remote_shell]
    ssh_env = os.environ.copy()
    # Use the interactive pane's agent socket when it is still live; otherwise
    # find any usable agent rather than handing ssh a stale socket (which makes
    # BatchMode auth flap and the emoji flicker). If none is live, drop the var
    # so ssh can fall back to on-disk IdentityFiles.
    agent = target.get('ssh_auth_sock')
    if not _is_live_socket(agent):
        agent = discover_live_agent_socket(ssh_env.get('SSH_AUTH_SOCK'))
    if agent:
        ssh_env['SSH_AUTH_SOCK'] = agent
    else:
        ssh_env.pop('SSH_AUTH_SOCK', None)
    try:
        proc = subprocess.run(cmd, input=REMOTE_COLLECT, capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=REMOTE_TIMEOUT, env=ssh_env)
    except FileNotFoundError:
        return [], 'ssh executable not found on base machine', 0
    except subprocess.TimeoutExpired:
        return [], f'SSH timed out after {REMOTE_TIMEOUT:g}s', 0
    except OSError as exc:
        return [], f'SSH launch error: {exc}', 0
    sockets = proc.stdout.count('\x1eAWSOCK\t')
    records = parse_remote_records(proc.stdout, session)
    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        # Do not log remote terminal screen contents or credentials.
        reason = err[-1][:300] if err else f'SSH/remote command exit {proc.returncode}'
        return [], reason, sockets
    if '\x1eAWEND\n' not in proc.stdout:
        return [], 'incomplete tmux inventory returned by SSH', sockets
    if session and not records:
        return [], f'no panes in remote session {session!r}', sockets
    return records, '', sockets


def remote_target(pane, pid, links):
    """Explicit pane links override best-effort automatic SSH discovery."""
    pinned = links.get(pane)
    if pinned:
        return pinned
    if not pid:
        return None
    return discovered_ssh(pid)


def remote_states(records, memory=None, target_key=None):
    """Recognize every remote pane; retain *unchanged* hidden pane state.

    A hidden TUI may stop rendering its footer. Keeping a previously observed
    state is reasonable only while its captured screen has not changed.
    Whenever fresh UI cues appear, they override the remembered state.
    """
    best = ('', 'no recognizable agent state in remote windows')
    counts = {'ready': 0, 'work': 0, 'wait': 0}
    details = []
    seen = set()
    for pane, sess, name, command, title, screen, socket_id, active, proc_agent in records:
        key = (target_key, socket_id, pane)
        seen.add(key)
        hint_command = command + ((' agentproc:' + proc_agent) if proc_agent else '')
        state, reason = classify(screen, name + ' ' + title, hint_command)
        fingerprint = hashlib.blake2s(screen.encode('utf-8', 'replace'), digest_size=12).digest()
        previous = memory.get(key) if memory is not None else None
        if not state and previous and active == '0' and previous[1] == fingerprint:
            state, reason = previous[0], 'unchanged hidden pane (last confirmed state)'
        if memory is not None:
            if state:
                memory[key] = (state, fingerprint)
            else:
                memory.pop(key, None)
        if state:
            counts[state] += 1
        if PRIORITY[state] > PRIORITY[best[0]]:
            best = (state, f'remote {sess}:{name} {pane}: {reason}')
        details.append({'pane':pane, 'session':sess, 'name':name,
                        'active':active, 'agent':proc_agent or '-',
                        'state':state or 'unknown', 'reason':reason})
    if memory is not None:
        for key in list(memory):
            if key[0] == target_key and key not in seen:
                del memory[key]
    return best, counts, details


def scan(socket, show=False, remote_cache=None, force_remote=False, remote_memory=None,
         remote_interval=None):
    panes = list_panes(socket)
    if panes is None:
        return None
    ri = remote_interval if (remote_interval and remote_interval >= 1.0) else REMOTE_INTERVAL
    if remote_cache is None:
        remote_cache = {}
    if remote_memory is None:
        remote_memory = {}
    now = time.monotonic()
    links = saved_remote_links()
    pids = pane_process_ids(socket)
    windows = {}
    pane_info = []
    targets = {}

    # This index is local-only and read-only; it associates the tmux clients
    # launched inside outer panes with other local tmux servers. It also
    # uncovers hidden SSH panes in those local nested servers.
    nested_index = local_nested_index(socket)

    def add_target(target):
        # The agent socket is deliberately NOT part of the identity: it can flap
        # (a path one tick, None the next) as /proc readability changes, and
        # including it would churn the remote_cache key and lose the grace window
        # that holds a hidden-window emoji steady across a dropped poll.
        key = (target['host'], tuple(target.get('opts', [])),
               target.get('session'), target.get('socket'))
        existing = targets.get(key)
        if existing is None:
            targets[key] = target
        elif target.get('ssh_auth_sock') and not existing.get('ssh_auth_sock'):
            existing['ssh_auth_sock'] = target['ssh_auth_sock']
        return key

    for pane, window, name, command in panes:
        found = []
        native = native_state(socket, pane)
        if native:
            found.append((native, 'local Claude Code native hook'))
        else:
            cap = tmux_call(socket, 'capture-pane', '-p', '-t', pane)
            if cap.returncode != 0:
                cap = tmux_call(socket, 'capture-pane', '-a', '-p', '-t', pane)
            if cap.returncode != 0:
                found.append(('', 'tmux capture-pane failed'))
            else:
                agent_cmd = local_agent_descendant(pids.get(pane))
                found.append(classify(cap.stdout, name + ' ' + agent_cmd, command))

        keys = []
        direct = 0
        nested_count = 0
        # Preserve explicit pane->host links; add auto-discovered hosts too.
        pinned = links.get(pane)
        if pinned:
            keys.append(add_target(pinned))
        elif not native:
            direct_targets = discovered_ssh_targets(pids.get(pane))
            direct = len(direct_targets)
            keys.extend(add_target(t) for t in direct_targets)

        # Local nested clients can have SSH processes that are NOT children of
        # the outer pane's shell. Inspect attached local tmux servers directly.
        if not native:
            for nested_sock, nested_panes, nested_pids in linked_nested_servers(
                    pids.get(pane), nested_index):
                nested_count += 1
                for npane, nwindow, nname, ncmd in nested_panes:
                    # The native Claude state belongs to the actual inner
                    # pane, not the outer tmux pane displaying the client.
                    # It remains available even when the nested window isn't
                    # selected or its TUI is no longer repainting.
                    nested_native = native_state(nested_sock, npane)
                    if nested_native:
                        found.append((nested_native, 'nested local Claude Code native hook'))
                    else:
                        ncap = tmux_call(nested_sock, 'capture-pane', '-p', '-t', npane)
                        if ncap.returncode != 0:
                            ncap = tmux_call(nested_sock, 'capture-pane', '-a', '-p', '-t', npane)
                        if ncap.returncode == 0:
                            agent_cmd = local_agent_descendant(nested_pids.get(npane))
                            found.append(classify(ncap.stdout, nname + ' ' + agent_cmd, ncmd))
                    for t in discovered_ssh_targets(nested_pids.get(npane)):
                        keys.append(add_target(t))
        pane_info.append((pane, window, name, command, found,
                          list(dict.fromkeys(keys)), direct, nested_count))

    # Probe distinct remote targets in parallel so a broken host does not
    # stall other panes. All remote tmux windows are captured independently
    # of which window the interactive remote client has selected.
    due = {key: target for key, target in targets.items()
           if force_remote or key not in remote_cache or now - remote_cache[key][0] >= ri}
    if due:
        with ThreadPoolExecutor(max_workers=min(8, len(due))) as pool:
            futures = {pool.submit(remote_probe, target): key for key, target in due.items()}
            for future in as_completed(futures):
                key = futures[future]
                try:
                    data, error, sockets = future.result()
                except Exception as exc:
                    data, error, sockets = [], f'remote probe error: {exc}', 0
                prev = remote_cache.get(key)
                if not error and data is not None:
                    remote_cache[key] = (now, data, '', sockets, now)
                elif prev and prev[4] is not None and prev[1] and now - prev[4] <= REMOTE_GRACE:
                    # Transient failure within grace: reuse the last good data.
                    remote_cache[key] = (now, prev[1], error, prev[3], prev[4])
                else:
                    remote_cache[key] = (now, [], error, sockets,
                                         prev[4] if prev else None)
    for key in list(remote_cache):
        if key not in targets:
            remote_cache.pop(key, None)

    for pane, window, name, command, found, keys, direct, nested_count in pane_info:
        if show:
            print(f'{pane} {name} ({command}): ssh_targets={len(keys)} '
                  f'direct_ssh={direct} local_nested_tmux={nested_count}')
        for key in keys:
            target = targets[key]
            _, data, error, sockets, _ = remote_cache.get(
                key, (0, [], 'not probed', 0, None))
            if error and not data:
                if show:
                    print(f'  remote={target["host"]} ERROR: {error}')
                    low = error.lower()
                    if 'permission denied' in low and 'password' in low:
                        print('    Needs non-interactive key auth: this pane\'s interactive '
                              'login used a password, but the watcher opens a SEPARATE SSH '
                              'connection (BatchMode) and cannot reuse it or type a password.')
                        print('    Fix: add a key the agent offers, e.g. '
                              f'`ssh-copy-id {target["host"]}`, then re-run check. '
                              '(Hidden remote windows stay unknown until key auth works; the '
                              'active window still shows via the local pane.)')
                    elif 'permission denied' in low:
                        print('    No key accepted for a non-interactive login. Add your public '
                              f'key to the remote ~/.ssh/authorized_keys (`ssh-copy-id {target["host"]}`).')
                    elif 'passphrase' in low:
                        print('    Key is passphrase-protected; load it into your agent first: `ssh-add`.')
                    elif 'no accessible tmux server sockets' in error:
                        print('    Check the remote tmux user, TMUX_TMPDIR or custom -S socket.')
                continue
            (rstate, rreason), counts, details = remote_states(data, remote_memory, key)
            if rstate:
                found.append((rstate, rreason + (' [stale: last probe failed]' if error else '')))
            if show:
                stale = f' (STALE, serving last good; last probe failed: {error})' if error else ''
                print(f'  remote={target["host"]} sockets={sockets} panes={len(data)} '
                      f'states={counts}{stale}')
                for detail in details:
                    print(f'    {detail["session"]}:{detail["name"]} {detail["pane"]} '
                          f'active={detail["active"]} agent={detail["agent"]} '
                          f'state={detail["state"]} reason={detail["reason"]}')
                if data and not any(counts.values()):
                    print('    Connected to remote tmux, but agent UI was not recognized.')
                elif not data:
                    print('    SSH connected, but no remote tmux panes were returned.')
        if show and not keys:
            print('  remote=not discovered. If this pane contains SSH in an '
                  'unreachable nested tmux, use: tmux-agent-watch link '
                  f'{pane} HOST')
        state, reason = max(found, key=lambda p: PRIORITY.get(p[0], 0)) if found else ('', 'no state')
        if window not in windows or PRIORITY[state] > PRIORITY[windows[window][0]]:
            windows[window] = (state, reason)
        if show:
            print(f'  => {pane}: {state or "unknown"} [{reason}]')
    return windows


def remote_interval_for(socket):
    """Per-tier override of the remote-probe cadence, live-tunable with:
        tmux set -g @agentwatch_remote_interval <seconds>
    Lower it on outer tiers (e.g. a jump box) so short-lived states propagate
    up faster, at the cost of more frequent SSH probes. Falls back to the
    REMOTE_INTERVAL default when unset or invalid."""
    out = tmux_call(socket, 'show-option', '-gv', '@agentwatch_remote_interval')
    try:
        val = float(out.stdout.strip())
        if val >= 1.0:
            return val
    except (ValueError, AttributeError):
        pass
    return REMOTE_INTERVAL


def run_watch(socket):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = state_file(socket)
    # flock prevents duplicate background observers on repeated tmux reloads.
    with path.open('a+') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()) + '\n')
        lock.flush()
        live = True
        def shutdown(signum, frame):
            nonlocal live
            live = False
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGINT, shutdown)
        old = {}
        remote_cache = {}
        remote_memory = {}
        hard_failures = 0
        try:
            while live:
                try:
                    windows = scan(socket, remote_cache=remote_cache,
                                   remote_memory=remote_memory,
                                   remote_interval=remote_interval_for(socket))
                except Exception:
                    # A transient error (tmux hiccup, an SSH teardown mid-probe,
                    # a parse race) must NEVER kill the daemon: dying would run
                    # the cleanup below and blank the whole status bar until the
                    # next tmux reload. Keep the last states and retry next tick.
                    time.sleep(INTERVAL)
                    continue
                if windows is None:
                    # list-panes failed: the outer tmux server is likely gone.
                    hard_failures += 1
                    if hard_failures >= 5:
                        break
                    time.sleep(INTERVAL)
                    continue
                hard_failures = 0
                for window in set(old) | set(windows):
                    state = windows.get(window, ('',))[0]
                    if old.get(window, '') != state:
                        set_state(socket, window, state)
                old = {win: pair[0] for win, pair in windows.items()}
                time.sleep(INTERVAL)
        finally:
            # Clear the indicators only on a clean, intentional stop (SIGTERM/
            # SIGINT from `stop`/uninstall set live=False). On a crash or a lost
            # tmux server, leave the last states in place so the bar does not
            # silently blank; a relaunched daemon re-sets them from fresh scans.
            if not live:
                for window in old:
                    set_state(socket, window, '')
            try:
                if path.read_text().strip() == str(os.getpid()):
                    path.unlink()
            except OSError:
                pass
    return 0


def start_watch(socket):
    if not socket:
        print('No running tmux server found. Watcher will start with tmux.', file=sys.stderr)
        return 0
    if running_pid(socket):
        return 0
    # Detached process: one lightweight Python watcher per outer tmux socket.
    subprocess.Popen([sys.executable, str(BIN), '_watch', socket],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True,
                     close_fds=True)
    return 0


def restart_watch(socket):
    # Reinstalling must replace an existing observer (code is in memory).
    if running_pid(socket):
        stop_watch(socket)
        # Wait for the old observer to release its exclusive lock before
        # launching the new observer, rather than racing and losing it.
        deadline = time.monotonic() + 5.0
        lock_path = state_file(socket)
        while time.monotonic() < deadline:
            try:
                with lock_path.open('a+') as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    break
            except (OSError, BlockingIOError):
                pass
            time.sleep(0.1)
    return start_watch(socket)


def stop_watch(socket):
    if not socket:
        return
    pid = running_pid(socket)
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    # Also clear the badges immediately, even if the watcher is still exiting.
    for window in set((p[1] for p in (list_panes(socket) or []))):
        set_state(socket, window, '')


def emit_event(state):
    """Called by locally-running Claude hooks, even with sanitized TMUX env."""
    if state not in ('ready', 'work', 'wait', 'clear'):
        return 0
    sock, pane = find_hook_pane()
    if not pane:
        return 0
    if state == 'clear':
        tmux_call(sock, 'set-option', '-p', '-u', '-t', pane,
                  '@agentwatch_native_state')
    else:
        tmux_call(sock, 'set-option', '-p', '-t', pane,
                  '@agentwatch_native_state', state)
    return 0


def claude_hook_specs():
    # Use native events only on the base host. Never SSH into remote hosts.
    # The shell expands $HOME, avoiding hard-coded paths in hooks.
    def hook(state):
        return {'hooks': [{'type': 'command',
                           'command': f'"${{HOME}}/.local/bin/tmux-agent-watch" event {state}',
                           'timeout': 5}]}
    return {
        'SessionStart': [hook('ready')],
        'UserPromptSubmit': [hook('work')],
        'PreToolUse': [{**hook('wait'), 'matcher': 'AskUserQuestion|ExitPlanMode'}],
        'PermissionRequest': [hook('wait')],
        'Notification': [{**hook('wait'), 'matcher': 'permission_prompt|elicitation_dialog'}],
        'Elicitation': [hook('wait')],
        'ElicitationResult': [hook('work')],
        'PostToolUse': [hook('work')],
        'PostToolUseFailure': [hook('work')],
        'Stop': [hook('ready')],
        'StopFailure': [hook('ready')],
        'SessionEnd': [hook('clear')],
    }


def is_our_hook_group(entry):
    if not isinstance(entry, dict):
        return False
    entries = entry.get('hooks', [])
    return (isinstance(entries, list)
            and any(isinstance(x, dict)
                    and CLAUDE_HOOK_MARKER in x.get('command', '')
                    for x in entries))


def update_claude_hooks(uninstall=False):
    path = CLAUDE_SETTINGS
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError) as exc:
            print(f'WARNING: Claude hook installation skipped: invalid {path}: {exc}', file=sys.stderr)
            return False
        if not isinstance(data, dict):
            print(f'WARNING: Claude hook installation skipped: {path} must contain an object.', file=sys.stderr)
            return False
    else:
        if uninstall:
            return False
        data = {}
    hooks = data.get('hooks', {})
    if not isinstance(hooks, dict):
        print(f'WARNING: Claude hook installation skipped: hooks in {path} is not an object.', file=sys.stderr)
        return False
    original = path.read_text(encoding='utf-8') if path.exists() else ''
    # Remove only our command entries; preserve other hooks in shared groups.
    for name, groups in list(hooks.items()):
        if not isinstance(groups, list):
            continue
        retained = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
                retained.append(group)
                continue
            commands = [h for h in group['hooks'] if not (isinstance(h, dict)
                         and CLAUDE_HOOK_MARKER in h.get('command', ''))]
            if commands:
                retained.append({**group, 'hooks': commands})
        if retained:
            hooks[name] = retained
        else:
            hooks.pop(name, None)
    if not uninstall:
        for event, groups in claude_hook_specs().items():
            hooks.setdefault(event, []).extend(groups)
    if hooks:
        data['hooks'] = hooks
    else:
        data.pop('hooks', None)
    updated = json.dumps(data, indent=2, ensure_ascii=False) + '\n'
    if uninstall and not path.exists():
        return False
    if updated == original:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + '.agentwatch.' + datetime.now().strftime('%Y%m%d-%H%M%S') + '.bak')
        shutil.copy2(path, backup)
        print(f'Backup: {backup}')
    tmp = path.with_name(path.name + '.agentwatch.tmp')
    tmp.write_text(updated, encoding='utf-8')
    os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    tmp.replace(path)
    return True


def reset_native(socket):
    for pane, _window, _name, _command in (list_panes(socket) or []):
        tmux_call(socket, 'set-option', '-p', '-u', '-t', pane,
                  '@agentwatch_native_state')


def config_paths():
    xdg = Path(os.environ.get('XDG_CONFIG_HOME', str(HOME / '.config'))) / 'tmux' / 'tmux.conf'
    choices = [HOME / '.tmux.conf', xdg]
    main = next((p for p in choices if p.exists()), choices[0])
    try:
        text = main.read_text(encoding='utf-8')
    except OSError:
        text = ''
    ohmy = 'gpakosz' in str(main.resolve()).lower() or 'oh my tmux' in text.lower()
    return main, (Path(str(main) + '.local') if ohmy else main), ohmy


def new_config_block(ohmy):
    # In oh-my-tmux .conf.local these are interpreted as shell assignments.
    if ohmy:
        base = (
            'tmux_conf_theme_window_status_format="${tmux_conf_theme_window_status_format}' + WINDOW_FLAG + '"\n'
            'tmux_conf_theme_window_status_current_format="${tmux_conf_theme_window_status_current_format}' + WINDOW_FLAG + '"\n'
        )
    else:
        base = (
            "set -g window-status-format '#I:#W" + WINDOW_FLAG + "'\n"
            "set -g window-status-current-format '#I:#W" + WINDOW_FLAG + "'\n"
        )
    return (MARK_START + '\n' + base +
            'run-shell -b \'exec "$HOME/.local/bin/tmux-agent-watch" start "#{socket_path}"\'\n' +
            MARK_END + '\n')


def update_config(path, ohmy, uninstall=False):
    try:
        original = path.read_text(encoding='utf-8') if path.exists() else ''
    except OSError as exc:
        raise RuntimeError(f'Cannot read {path}: {exc}') from exc
    cleaned = re.sub(r'(?m)^' + re.escape(MARK_START) + r'\n.*?^' + re.escape(MARK_END) + r'\n?',
                     '', original, flags=re.S | re.M)
    if uninstall:
        updated = cleaned
    else:
        block = new_config_block(ohmy)
        # Oh-my-tmux's config reserves the text after # EOF for shell functions.
        match = re.search(r'(?m)^# EOF\s*$', cleaned) if ohmy else None
        if match:
            updated = cleaned[:match.start()].rstrip('\n') + '\n\n' + block + '\n' + cleaned[match.start():]
        else:
            updated = cleaned.rstrip('\n') + '\n\n' + block
    if updated == original:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup = path.with_name(path.name + '.agentwatch.' + stamp + '.bak')
        shutil.copy2(path, backup)
        print(f'Backup: {backup}')
    tmp = path.with_name(path.name + '.agentwatch.tmp')
    tmp.write_text(updated, encoding='utf-8')
    os.chmod(tmp, path.stat().st_mode & 0o777 if path.exists() else 0o600)
    tmp.replace(path)
    return True


def install(uninstall=False):
    if os.geteuid() == 0 and HOME == Path('/root'):
        print('Run as your normal user, not root/sudo.', file=sys.stderr)
        return 2
    main, cfg, ohmy = config_paths()
    sock = socket_guess()
    if not uninstall:
        if shutil.which('tmux') is None:
            print('tmux is not installed or not on PATH.', file=sys.stderr)
            return 2
        BIN.parent.mkdir(parents=True, exist_ok=True)
        source = Path(__file__).resolve()
        if source != BIN:
            shutil.copy2(source, BIN)
        BIN.chmod(0o755)
        update_config(cfg, ohmy, False)
        updated_hooks = update_claude_hooks(False)
        print(f'Installed on this machine only: {BIN}')
        print(f'tmux config: {cfg} ({"oh-my-tmux" if ohmy else "standard tmux"})')
        if sock:
            # Sourcing the main config applies the oh-my-tmux theme variables.
            r = tmux_call(sock, 'source-file', str(main), timeout=12)
            if r.returncode != 0:
                print('Could not reload tmux. Use your tmux reload shortcut. ' + r.stderr.strip()[:200])
            restart_watch(sock)
        else:
            print('Watcher will start automatically when you start tmux.')
        print('Indicators: ✋ input needed; 👨‍💻 working; 🧑‍🔧 ready/idle.')
        print('Local Claude Code: native hooks installed; restart Claude to load them.' if updated_hooks else 'Local Claude Code: native hook configuration unchanged or unavailable.')
        print('Remote nested tmux: inspects hidden windows, with per-pane agent process identification.')
        print(f'Diagnostics: {BIN} check')
        print(f'Uninstall: python3 {source} --uninstall')
    else:
        stop_watch(sock)
        reset_native(sock)
        update_config(cfg, ohmy, True)
        update_claude_hooks(True)
        if REMOTE_LINKS.exists():
            REMOTE_LINKS.unlink()
        if sock:
            r = tmux_call(sock, 'source-file', str(main), timeout=12)
            if r.returncode != 0:
                print('Reload tmux config manually to restore original status format.')
        if BIN.exists():
            BIN.unlink()
        print('Removed watcher, tmux configuration, and local Claude hooks.')
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('command', nargs='?', default='install',
                    choices=['install', 'start', 'stop', 'check', 'event', 'reset', 'link', 'unlink', 'links', '_watch'])
    ap.add_argument('socket', nargs='?')
    ap.add_argument('extra', nargs='*')
    ap.add_argument('--uninstall', action='store_true')
    args = ap.parse_args()
    if args.command == 'link':
        # Usage: tmux-agent-watch link %5 gpu-server [tmux-session] [socket-path]
        if not args.socket or not args.extra:
            ap.error('usage: tmux-agent-watch link %PANE SSH_ALIAS [TMUX_SESSION] [REMOTE_SOCKET]')
        pane, host = args.socket, args.extra[0]
        if not re.fullmatch(r'%[0-9]+', pane) or not re.fullmatch(r'[\w.\-:@\[\]%]+', host):
            ap.error('expected a tmux pane ID and a valid SSH host/alias')
        link = {'host': host, 'opts': []}
        if len(args.extra) >= 2 and args.extra[1] != '-':
            link['session'] = args.extra[1]
        if len(args.extra) >= 3:
            link['socket'] = args.extra[2]
        links = saved_remote_links()
        links[pane] = link
        save_remote_links(links)
        print(f'Linked outer pane {pane} to {host}' +
              (f' (session {link["session"]})' if link.get('session') else ' (all sessions)') +
              (f' (socket {link["socket"]})' if link.get('socket') else ''))
        return 0
    if args.command == 'unlink':
        if not args.socket:
            ap.error('usage: tmux-agent-watch unlink %PANE')
        links = saved_remote_links()
        links.pop(args.socket, None)
        save_remote_links(links)
        print(f'Unlinked pane {args.socket}')
        return 0
    if args.command == 'links':
        print(json.dumps(saved_remote_links(), indent=2))
        return 0
    if args.command == 'event':
        return emit_event(args.socket or '')
    if args.command == 'reset':
        socket = args.socket or socket_guess()
        if socket:
            reset_native(socket)
        print('Cleared local native agent states.')
        return 0
    if args.command == '_watch':
        return run_watch(args.socket) if args.socket else 2
    if args.command == 'start':
        return start_watch(args.socket or socket_guess())
    if args.command == 'stop':
        return stop_watch(args.socket or socket_guess()) or 0
    if args.command == 'check':
        socket = args.socket or socket_guess()
        if not socket:
            print('No reachable tmux server.')
            return 1
        print(f'Watcher PID: {running_pid(socket) or "not running"}')
        scan(socket, show=True, force_remote=True)
        return 0
    return install(args.uninstall)


if __name__ == '__main__':
    sys.exit(main())
