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

  # Swift helpers + deploy/todoist-sync.app, running this venv's Python.
  scripts.build-app.exec = ''
    TODOIST_SYNC_PYTHON="$DEVENV_STATE/venv/bin/python" "$DEVENV_ROOT/deploy/build-app.sh"
  '';

  # Runs one sync through the app bundle, so macOS attributes Reminders
  # access to Todoist Sync rather than the calling terminal. Output goes to
  # var/sync.log (see sync-log). Pass --dry-run to preview.
  scripts.sync.exec = ''
    open -n -W "$DEVENV_ROOT/deploy/todoist-sync.app" --args "$@"
  '';

  scripts.sync-log.exec = ''
    tail -n 40 -F "$DEVENV_ROOT/var/sync.log"
  '';

  # `python -m` puts the repo root on sys.path; the tests import tests.conftest.
  enterTest = ''
    python -m pytest -q
  '';
}
