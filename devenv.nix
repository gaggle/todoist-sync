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

  scripts.build-bridge.exec = ''
    cd "$DEVENV_ROOT/swift/reminders-bridge" && swift build -c release
  '';

  scripts.sync.exec = ''
    python -m todoist_sync.sync_tasks "$@"
  '';

  # `python -m` puts the repo root on sys.path; the tests import tests.conftest.
  enterTest = ''
    python -m pytest -q
  '';
}
