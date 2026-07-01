import os
import subprocess
import textwrap
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]


def test_infra_absent_pg_uses_compose_creation_not_docker_start(tmp_path):
    """Regression: absent bogo-pg must be created by compose, not started by name.

    Bug was: bogo-pg state=absent flowed into `docker start bogo-pg`, which then
    failed with "No such container" and was reported as a Docker-not-ready blocker.
    """
    calls = tmp_path / "docker.calls"
    marker = tmp_path / "compose.created"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker = fake_bin / "docker"
    docker.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            set -euo pipefail
            calls={str(calls)!r}
            marker={str(marker)!r}
            printf '%s\\n' "$*" >> "$calls"

            case "$1" in
              info)
                exit 0
                ;;
              compose)
                if [ "${{2:-}}" = "version" ]; then
                  exit 0
                fi
                if [ "${{6:-}}" = "up" ] && [ "${{7:-}}" = "-d" ]; then
                  touch "$marker"
                  exit 0
                fi
                exit 1
                ;;
              inspect)
                fmt="${{3:-}}"
                name="${{4:-}}"
                case "$fmt" in
                  *State.Status*)
                    if [ "$name" = "bogo-pg" ] && [ ! -f "$marker" ]; then
                      exit 1
                    fi
                    echo running
                    ;;
                  *HostConfig.RestartPolicy.Name*)
                    echo unless-stopped
                    ;;
                  *NetworkSettings.Networks*Aliases*)
                    echo "bogo-pg hermes-pg"
                    ;;
                  *NetworkSettings.Networks*)
                    echo "bogo-net"
                    ;;
                  *State.Health*)
                    if [ "$name" = "bogo-mm" ]; then
                      echo healthy
                    else
                      echo none
                    fi
                    ;;
                  *)
                    echo ""
                    ;;
                esac
                exit 0
                ;;
              exec)
                echo 200
                exit 0
                ;;
              start)
                echo "No such container: $2" >&2
                exit 1
                ;;
              update|network)
                exit 0
                ;;
            esac
            exit 1
            """
        ),
        encoding="utf-8",
    )
    docker.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "BOGO_SKIP_PROVISION": "1",
            "BOGO_MM_WAIT_TIMEOUT": "1",
        }
    )

    result = subprocess.run(
        ["bash", str(APP_DIR / "infra_up.sh")],
        cwd=APP_DIR,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
    )

    call_log = calls.read_text(encoding="utf-8")
    assert result.returncode == 0, result.stdout
    assert "Detected missing containers" in result.stdout
    assert "Container creation/startup via compose complete" in result.stdout
    assert "compose --project-directory" in call_log
    assert "up -d" in call_log
    assert "start bogo-pg" not in call_log
    assert "No such container" not in result.stdout


def test_oneclick_only_labels_rc2_as_docker_runtime_blocker():
    """Only infra rc=2 should become the user-facing Docker-not-ready guidance."""
    oneclick = (APP_DIR / "bogo_oneclick.sh").read_text(encoding="utf-8")
    assert 'return "$rc"' in oneclick
    assert "see the infra error above for the real blocker" in oneclick
    assert 'if [ "$infra_rc" -eq 2 ]; then' in oneclick
    assert "Cause: the Docker daemon is not ready or this user cannot access /var/run/docker.sock." in oneclick
    assert "Cause: the Docker/Colima runtime is not ready." in oneclick
    assert "systemctl --user status bogo@dashboard.service" in oneclick


def test_linux_service_installer_preflights_user_systemd():
    """Linux startup should fail with an actionable message if systemd --user is unavailable."""
    installer = (APP_DIR / "service" / "install_service.sh").read_text(encoding="utf-8")
    assert "linux_require_systemd_user()" in installer
    assert "systemctl --user show-environment" in installer
    assert "systemd --user is not reachable for this login session." in installer
    assert "linux_require_systemd_user" in installer
