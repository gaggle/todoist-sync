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

  # Build an environment app: `env-build staging` (live repo code) or
  # `env-build prod` (snapshot of the committed HEAD). See deploy/env.sh.
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

  scripts.env-log.exec = ''
    tail -n 40 -F "$HOME/Library/Logs/Todoist Sync/''${1:?usage: env-log staging|prod}.log"
  '';

  # `python -m` puts the repo root on sys.path; the tests import tests.conftest.
  enterTest = ''
    python -m pytest -q
  '';
}
