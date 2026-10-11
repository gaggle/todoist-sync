{ pkgs, config, ... }:

{
  # The Swift helpers build with the system toolchain (Xcode Command Line
  # Tools). Nix's Apple SDK would override SDKROOT/DEVELOPER_DIR and break
  # `swift build`, so leave the SDK to the system.
  apple.sdk = null;

  languages.python = {
    enable = true;
    package = pkgs.python312;
    venv.enable = true;
    venv.requirements = ''
      ${builtins.readFile ./requirements.txt}
      pytest
      -e ${config.devenv.root}
    '';
  };

  # Build an environment app: `env-build staging [--agents]` (live repo code)
  # or `env-build prod [tag]` (a worktree at a tag, synced in the background).
  # See deploy/env.sh.
  scripts.env-build.exec = ''
    "$DEVENV_ROOT/deploy/env.sh" "$@"
  '';

  # One sync through an environment's app — so macOS attributes Reminders
  # access to that app — e.g. `env-sync staging --dry-run`.
  scripts.env-sync.exec = ''
    env="$1"; shift
    case "$env" in staging) name="Todoist Sync (Staging)" ;; prod) name="Todoist Sync" ;; *) echo "usage: env-sync staging|prod [--dry-run]" >&2; exit 1 ;; esac
    open -n -W "$HOME/Applications/$name.app" --args "$@"
  '';

  # Simulates a wake (SIGUSR1 to the wake-watcher, which treats it like the
  # real thing) and checks that it started a sync. The only way to test the
  # wake path on a Mac that never sleeps. Needs the env's agents loaded.
  scripts.env-wake-test.exec = ''
    env="''${1:?usage: env-wake-test staging|prod}"
    job="gui/$(id -u)/com.todoist-sync.$env"
    wakelog="$HOME/Library/Logs/Todoist Sync/$env.launchd.log"
    launchctl print "$job.wake-watcher" >/dev/null 2>&1 || { echo "error: $env's wake-watcher isn't loaded (staging: env-build staging --agents)" >&2; exit 1; }
    runs() { launchctl print "$job.sync" | awk '/^\truns = /{print $3}'; }
    # A kickstart while a sync is running does nothing, so start from idle.
    while launchctl print "$job.sync" | grep -q "^	state = running"; do sleep 1; done
    before="$(runs)"; lines="$(wc -l < "$wakelog" 2>/dev/null || echo 0)"
    launchctl kill SIGUSR1 "$job.wake-watcher"
    for _ in $(seq 1 45); do
      if [ "$(runs)" -gt "$before" ] && tail -n +"$((lines + 1))" "$wakelog" | grep -q "simulated wake"; then
        tail -n +"$((lines + 1))" "$wakelog" | grep "simulated wake"
        echo "ok: a simulated wake started a $env sync"
        exit 0
      fi
      sleep 1
    done
    echo "FAIL: no sync started within 45s of a simulated wake; see $wakelog" >&2
    exit 1
  '';

  scripts.env-log.exec = ''
    tail -n 40 -F "$HOME/Library/Logs/Todoist Sync/''${1:?usage: env-log staging|prod}.log"
  '';

  # End-to-end scenarios against the "Sync Test" list and project, run as the
  # staging app (see integration_tests/conftest.py). Extra args go to pytest.
  scripts.integration-test.exec = ''
    if launchctl print "gui/$(id -u)/com.todoist-sync.staging.sync" >/dev/null 2>&1; then
      echo "error: staging is syncing in the background, which would interfere — remove its agents first: env-build staging" >&2
      exit 1
    fi
    data="$HOME/Library/Application Support/Todoist Sync/staging"
    out="$(mktemp -d)"
    rm -f "$data/integration-tests.exit"
    open -n -W --stdout "$out/stdout" --stderr "$out/stderr" \
      "$HOME/Applications/Todoist Sync (Staging).app" --args --integration-tests "$@"
    cat "$out/stdout" "$out/stderr"
    exit "$(cat "$data/integration-tests.exit" 2>/dev/null || echo 1)"
  '';

  # `python -m` puts the repo root on sys.path; the tests import tests.conftest.
  enterTest = ''
    python -m pytest -q
  '';
}
