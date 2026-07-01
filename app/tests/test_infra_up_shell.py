import hashlib
import os
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path

try:
    from packaging.markers import default_environment
    from packaging.requirements import Requirement
except ModuleNotFoundError:  # pragma: no cover - pytest commonly brings packaging, pip vendors it otherwise.
    from pip._vendor.packaging.markers import default_environment
    from pip._vendor.packaging.requirements import Requirement


APP_DIR = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, body: str) -> None:
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    path.chmod(0o755)


def _write_running_colima(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "colima",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "status" ]; then
          exit 0
        fi
        exit 0
        """,
    )


def _write_linux_uname(fake_bin: Path) -> None:
    _write_executable(
        fake_bin / "uname",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-s" ]; then
          printf 'Linux\n'
        else
          printf 'Linux\n'
        fi
        """,
    )


def test_infra_up_creates_absent_containers_via_compose(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_running_colima(fake_bin)

    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        state="${FAKE_DOCKER_STATE_DIR}/created"
        log="${FAKE_DOCKER_STATE_DIR}/calls.log"
        printf '%s\n' "$*" >> "$log"

        cmd="${1:-}"
        if [ "$cmd" = "info" ]; then
          exit 0
        fi

        if [ "$cmd" = "compose" ]; then
          if [ "${2:-}" = "version" ]; then
            exit 0
          fi
          if [ "${*: -2}" = "up -d" ]; then
            touch "$state"
            exit 0
          fi
        fi

        if [ "$cmd" = "inspect" ]; then
          fmt=""
          shift
          if [ "${1:-}" = "-f" ]; then
            fmt="$2"
            shift 2
          fi
          case "$fmt" in
            *State.Status*)
              if [ -f "$state" ]; then echo running; else echo absent; fi
              exit 0 ;;
            *State.Health*)
              echo healthy
              exit 0 ;;
            *HostConfig.RestartPolicy.Name*)
              echo unless-stopped
              exit 0 ;;
            *NetworkSettings.Networks*)
              if [[ "$fmt" == *Aliases* ]]; then
                echo "bogo-pg hermes-pg"
              else
                echo "bogo-net "
              fi
              exit 0 ;;
          esac
        fi

        if [ "$cmd" = "port" ]; then
          echo "127.0.0.1:8065"
          exit 0
        fi

        if [ "$cmd" = "update" ] || [ "$cmd" = "network" ] || [ "$cmd" = "exec" ]; then
          exit 0
        fi

        printf 'unexpected docker args: %s\n' "$*" >&2
        exit 99
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_DOCKER_STATE_DIR": str(state_dir),
            "BOGO_SKIP_PROVISION": "1",
            "BOGO_MM_WAIT_TIMEOUT": "1",
        }
    )

    proc = subprocess.run(
        ["bash", str(APP_DIR / "infra_up.sh")],
        cwd=APP_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Detected missing containers" in proc.stdout
    assert "Backbone container creation/startup complete." in proc.stdout
    calls = (state_dir / "calls.log").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("compose --project-directory ") and line.endswith(" up -d") for line in calls)
    assert "start bogo-pg" not in calls


def test_infra_up_normalizes_blank_inspect_failure_to_absent(tmp_path: Path) -> None:
    """Regression: some Docker CLIs can leave a blank stdout line before inspect fails.

    Without normalization, command substitution produced a value like "\nabsent",
    missing the exact `absent)` case and falling through to `docker start bogo-pg`.
    """
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_running_colima(fake_bin)

    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        state="${FAKE_DOCKER_STATE_DIR}/created"
        log="${FAKE_DOCKER_STATE_DIR}/calls.log"
        printf '%s\n' "$*" >> "$log"

        cmd="${1:-}"
        if [ "$cmd" = "info" ]; then
          exit 0
        fi

        if [ "$cmd" = "compose" ]; then
          if [ "${2:-}" = "version" ]; then
            exit 0
          fi
          if [ "${*: -2}" = "up -d" ]; then
            touch "$state"
            exit 0
          fi
        fi

        if [ "$cmd" = "inspect" ]; then
          fmt=""
          shift
          if [ "${1:-}" = "-f" ]; then
            fmt="$2"
            shift 2
          fi
          name="${1:-}"
          case "$fmt" in
            *State.Status*)
              if [ "$name" = "bogo-pg" ] && [ ! -f "$state" ]; then
                printf '\n'
                exit 1
              fi
              echo running
              exit 0 ;;
            *State.Health*)
              echo healthy
              exit 0 ;;
            *HostConfig.RestartPolicy.Name*)
              echo unless-stopped
              exit 0 ;;
            *NetworkSettings.Networks*)
              if [[ "$fmt" == *Aliases* ]]; then
                echo "bogo-pg hermes-pg"
              else
                echo "bogo-net "
              fi
              exit 0 ;;
          esac
        fi

        if [ "$cmd" = "port" ]; then
          echo "127.0.0.1:8065"
          exit 0
        fi

        if [ "$cmd" = "update" ] || [ "$cmd" = "network" ] || [ "$cmd" = "exec" ]; then
          exit 0
        fi

        printf 'unexpected docker args: %s\n' "$*" >&2
        exit 99
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_DOCKER_STATE_DIR": str(state_dir),
            "BOGO_SKIP_PROVISION": "1",
            "BOGO_MM_WAIT_TIMEOUT": "1",
        }
    )

    proc = subprocess.run(
        ["bash", str(APP_DIR / "infra_up.sh")],
        cwd=APP_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Detected missing containers" in proc.stdout
    assert "state=\nabsent" not in proc.stdout
    calls = (state_dir / "calls.log").read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("compose --project-directory ") and line.endswith(" up -d") for line in calls)
    assert "start bogo-pg" not in calls


def test_infra_up_uses_plain_docker_fallback_when_compose_is_missing(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_running_colima(fake_bin)

    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        pg_state="${FAKE_DOCKER_STATE_DIR}/pg"
        mm_state="${FAKE_DOCKER_STATE_DIR}/mm"
        log="${FAKE_DOCKER_STATE_DIR}/calls.log"
        printf '%s\n' "$*" >> "$log"

        cmd="${1:-}"
        if [ "$cmd" = "info" ]; then
          exit 0
        fi

        if [ "$cmd" = "compose" ] && [ "${2:-}" = "version" ]; then
          exit 127
        fi

        if [ "$cmd" = "network" ] || [ "$cmd" = "volume" ]; then
          exit 0
        fi

        if [ "$cmd" = "image" ] && [ "${2:-}" = "inspect" ]; then
          exit 1
        fi

        if [ "$cmd" = "pull" ]; then
          exit 0
        fi

        if [ "$cmd" = "run" ]; then
          name=""
          prev=""
          for arg in "$@"; do
            if [ "$prev" = "--name" ]; then
              name="$arg"
              break
            fi
            prev="$arg"
          done
          case "$name" in
            bogo-pg) touch "$pg_state" ;;
            bogo-mm) touch "$mm_state" ;;
            *) echo "missing --name in docker run" >&2; exit 99 ;;
          esac
          echo "fake-$name"
          exit 0
        fi

        if [ "$cmd" = "inspect" ]; then
          fmt=""
          shift
          if [ "${1:-}" = "-f" ]; then
            fmt="$2"
            shift 2
          fi
          name="${1:-}"
          case "$fmt" in
            *State.Status*)
              case "$name" in
                bogo-pg) [ -f "$pg_state" ] && echo running || echo absent ;;
                bogo-mm) [ -f "$mm_state" ] && echo running || echo absent ;;
                *) echo absent ;;
              esac
              exit 0 ;;
            *State.Health*)
              echo healthy
              exit 0 ;;
            *HostConfig.RestartPolicy.Name*)
              echo unless-stopped
              exit 0 ;;
            *NetworkSettings.Networks*)
              if [[ "$fmt" == *Aliases* ]]; then
                echo "bogo-pg hermes-pg"
              else
                echo "bogo-net "
              fi
              exit 0 ;;
          esac
        fi

        if [ "$cmd" = "exec" ]; then
          exit 0
        fi

        if [ "$cmd" = "port" ]; then
          echo "${MM_BIND_HOST:-127.0.0.1}:8065"
          exit 0
        fi

        if [ "$cmd" = "update" ]; then
          exit 0
        fi

        exit 99
        """,
    )
    _write_executable(
        fake_bin / "docker-compose",
        r"""
        #!/usr/bin/env bash
        exit 127
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_DOCKER_STATE_DIR": str(state_dir),
            "BOGO_SKIP_PROVISION": "1",
            "BOGO_MM_WAIT_TIMEOUT": "1",
            "MM_BIND_HOST": "172.16.100.200",
            "MM_SITE_URL": "http://172.16.100.200:8065",
        }
    )

    proc = subprocess.run(
        ["bash", str(APP_DIR / "infra_up.sh")],
        cwd=APP_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Docker Compose is not available" in proc.stdout
    assert "plain Docker CLI fallback" in proc.stdout
    assert "Pulling Docker image: postgres:15-alpine" in proc.stdout
    assert "Pulling Docker image: mattermost/mattermost-team-edition:9.11" in proc.stdout
    assert "Backbone container creation/startup complete." in proc.stdout
    calls = (state_dir / "calls.log").read_text(encoding="utf-8")
    assert "compose version" in calls
    assert "image inspect postgres:15-alpine" in calls
    assert "pull postgres:15-alpine" in calls
    assert "image inspect mattermost/mattermost-team-edition:9.11" in calls
    assert "pull mattermost/mattermost-team-edition:9.11" in calls
    assert "network inspect bogo-net" in calls
    assert "volume inspect bogo-pg-data" in calls
    assert "run -d --name bogo-pg" in calls
    assert "run -d --name bogo-mm" in calls
    assert "-p 172.16.100.200:8065:8065" in calls
    assert "MM_SERVICESETTINGS_SITEURL=http://172.16.100.200:8065" in calls


def test_infra_up_uses_docker_cli_when_existing_pg_has_no_healthcheck(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "pg").write_text("legacy", encoding="utf-8")
    _write_running_colima(fake_bin)

    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        pg_state="${FAKE_DOCKER_STATE_DIR}/pg"
        mm_state="${FAKE_DOCKER_STATE_DIR}/mm"
        net_state="${FAKE_DOCKER_STATE_DIR}/pg-net"
        log="${FAKE_DOCKER_STATE_DIR}/calls.log"
        printf '%s\n' "$*" >> "$log"

        cmd="${1:-}"
        if [ "$cmd" = "info" ]; then
          exit 0
        fi

        if [ "$cmd" = "compose" ]; then
          if [ "${2:-}" = "version" ]; then
            exit 0
          fi
          echo "compose up should not run for legacy PG without healthcheck" >&2
          exit 88
        fi

        if [ "$cmd" = "network" ]; then
          if [ "${2:-}" = "inspect" ]; then
            exit 0
          fi
          if [ "${2:-}" = "connect" ]; then
            touch "$net_state"
            exit 0
          fi
        fi

        if [ "$cmd" = "volume" ] || { [ "$cmd" = "image" ] && [ "${2:-}" = "inspect" ]; }; then
          exit 0
        fi

        if [ "$cmd" = "run" ]; then
          name=""
          prev=""
          for arg in "$@"; do
            if [ "$prev" = "--name" ]; then
              name="$arg"
              break
            fi
            prev="$arg"
          done
          [ "$name" = "bogo-mm" ] || { echo "unexpected run target: $name" >&2; exit 99; }
          touch "$mm_state"
          echo "fake-bogo-mm"
          exit 0
        fi

        if [ "$cmd" = "inspect" ]; then
          fmt=""
          shift
          if [ "${1:-}" = "-f" ]; then
            fmt="$2"
            shift 2
          fi
          name="${1:-}"
          case "$fmt" in
            *State.Status*)
              case "$name" in
                bogo-pg) [ -f "$pg_state" ] && echo running || echo absent ;;
                bogo-mm) [ -f "$mm_state" ] && echo running || echo absent ;;
                *) echo absent ;;
              esac
              exit 0 ;;
            *State.Health*)
              if [ "$name" = "bogo-pg" ]; then
                echo none
              else
                echo healthy
              fi
              exit 0 ;;
            *HostConfig.RestartPolicy.Name*)
              echo unless-stopped
              exit 0 ;;
            *Config.Env*)
              echo "MM_SERVICESETTINGS_SITEURL=${MM_SITE_URL:-http://127.0.0.1:8065}"
              echo "MM_SERVICESETTINGS_ALLOWCORSFROM="
              exit 0 ;;
            *NetworkSettings.Networks*)
              if [ "$name" = "bogo-pg" ] && [ ! -f "$net_state" ]; then
                echo ""
              elif [[ "$fmt" == *Aliases* ]]; then
                echo "bogo-pg hermes-pg"
              else
                echo "bogo-net "
              fi
              exit 0 ;;
          esac
        fi

        if [ "$cmd" = "exec" ] || [ "$cmd" = "update" ]; then
          exit 0
        fi

        if [ "$cmd" = "port" ]; then
          echo "${MM_BIND_HOST:-127.0.0.1}:8065"
          exit 0
        fi

        echo "unexpected docker args: $*" >&2
        exit 99
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_DOCKER_STATE_DIR": str(state_dir),
            "BOGO_SKIP_PROVISION": "1",
            "BOGO_MM_WAIT_TIMEOUT": "1",
        }
    )

    proc = subprocess.run(
        ["bash", str(APP_DIR / "infra_up.sh")],
        cwd=APP_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "without a Docker healthcheck" in output
    assert "plain Docker CLI fallback" in output
    calls = (state_dir / "calls.log").read_text(encoding="utf-8")
    assert "compose --project-directory" not in calls
    assert "network connect --alias hermes-pg --alias bogo-pg bogo-net bogo-pg" in calls
    assert "run -d --name bogo-mm" in calls


def test_infra_up_reconciles_stale_mm_bind_host_for_lan_mode(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _write_running_colima(fake_bin)

    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        set -euo pipefail
        marker="${FAKE_DOCKER_STATE_DIR}/compose-up"
        log="${FAKE_DOCKER_STATE_DIR}/calls.log"
        printf '%s\n' "$*" >> "$log"

        cmd="${1:-}"
        if [ "$cmd" = "info" ]; then
          exit 0
        fi

        if [ "$cmd" = "compose" ]; then
          if [ "${2:-}" = "version" ]; then
            exit 0
          fi
          if [ "${*: -2}" = "up -d" ]; then
            touch "$marker"
            exit 0
          fi
        fi

        if [ "$cmd" = "port" ]; then
          if [ -f "$marker" ]; then
            echo "172.16.100.200:8065"
          else
            echo "127.0.0.1:8065"
          fi
          exit 0
        fi

        if [ "$cmd" = "inspect" ]; then
          fmt=""
          shift
          if [ "${1:-}" = "-f" ]; then
            fmt="$2"
            shift 2
          fi
          case "$fmt" in
            *State.Status*)
              echo running
              exit 0 ;;
            *State.Health*)
              echo healthy
              exit 0 ;;
            *HostConfig.RestartPolicy.Name*)
              echo unless-stopped
              exit 0 ;;
            *Config.Env*)
              echo "MM_SERVICESETTINGS_SITEURL=http://172.16.100.200:8065"
              echo "MM_SERVICESETTINGS_ALLOWCORSFROM="
              exit 0 ;;
            *NetworkSettings.Networks*)
              if [[ "$fmt" == *Aliases* ]]; then
                echo "bogo-pg hermes-pg"
              else
                echo "bogo-net "
              fi
              exit 0 ;;
          esac
        fi

        if [ "$cmd" = "update" ] || [ "$cmd" = "network" ] || [ "$cmd" = "exec" ]; then
          exit 0
        fi

        printf 'unexpected docker args: %s\n' "$*" >&2
        exit 99
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}",
            "FAKE_DOCKER_STATE_DIR": str(state_dir),
            "BOGO_SKIP_PROVISION": "1",
            "BOGO_MM_WAIT_TIMEOUT": "1",
            "MM_BIND_HOST": "172.16.100.200",
            "MM_SITE_URL": "http://172.16.100.200:8065",
        }
    )

    proc = subprocess.run(
        ["bash", str(APP_DIR / "infra_up.sh")],
        cwd=APP_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "published port does not match MM_BIND_HOST=172.16.100.200" in proc.stdout
    assert "Compose configuration reconciled with .env." in proc.stdout
    calls = (state_dir / "calls.log").read_text(encoding="utf-8")
    assert "compose --project-directory" in calls
    assert " up -d" in calls


def test_infra_up_linux_reports_docker_daemon_not_running_without_socket(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_linux_uname(fake_bin)
    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "info" ]; then
          exit 1
        fi
        exit 99
        """,
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
            "BOGO_DOCKER_SOCK": str(tmp_path / "missing-docker.sock"),
        }
    )

    proc = subprocess.run(
        ["bash", str(APP_DIR / "infra_up.sh")],
        cwd=APP_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "colima not found (assuming Linux native docker)" in proc.stdout
    assert "Cannot connect to the Docker daemon (likely not running)." in proc.stderr
    assert "sudo systemctl start docker" in proc.stderr


def test_infra_up_linux_reports_docker_socket_permission_when_socket_exists(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_linux_uname(fake_bin)
    _write_executable(
        fake_bin / "docker",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "info" ]; then
          exit 1
        fi
        exit 99
        """,
    )
    sock_path = Path(f"/tmp/bg{os.getpid()}.sock")
    sock_path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(sock_path))
        server.listen(1)

        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin",
                "BOGO_DOCKER_SOCK": str(sock_path),
            }
        )

        proc = subprocess.run(
            ["bash", str(APP_DIR / "infra_up.sh")],
            cwd=APP_DIR,
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
    finally:
        server.close()
        sock_path.unlink(missing_ok=True)

    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "Docker socket access denied" in proc.stderr
    assert "sudo usermod -aG docker" in proc.stderr


def test_oneclick_propagates_infra_rc2_and_rc3_without_relabeling(tmp_path: Path) -> None:
    for rc, expected in [
        (2, "Cause: the Docker/Colima runtime is not ready."),
        (3, "Cause: Docker Compose is explicitly required"),
    ]:
        app_dir = tmp_path / f"app-rc{rc}"
        shutil.copytree(APP_DIR, app_dir, ignore=shutil.ignore_patterns(".venv", "logs", "__pycache__"))
        venv_bin = app_dir / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        _write_executable(
            venv_bin / "python",
            r"""
            #!/usr/bin/env bash
            exit 0
            """,
        )
        _write_executable(
            app_dir / "infra_up.sh",
            f"""
            #!/usr/bin/env bash
            echo "fake infra rc {rc}" >&2
            exit {rc}
            """,
        )

        proc = subprocess.run(
            ["bash", str(app_dir / "bogo_oneclick.sh"), "start"],
            cwd=app_dir,
            env={**os.environ, "BOGO_MM_WAIT_TIMEOUT": "1"},
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        assert proc.returncode == rc, proc.stdout + proc.stderr
        assert expected in proc.stderr
        assert "fake infra rc" in proc.stderr


def test_oneclick_propagates_linux_user_systemd_rc4(tmp_path: Path) -> None:
    app_dir = tmp_path / "app-rc4"
    shutil.copytree(APP_DIR, app_dir, ignore=shutil.ignore_patterns(".venv", "logs", "__pycache__"))
    venv_bin = app_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_executable(
        venv_bin / "python",
        """
        #!/usr/bin/env bash
        exit 0
        """,
    )
    _write_executable(
        app_dir / "infra_up.sh",
        """
        #!/usr/bin/env bash
        exit 0
        """,
    )
    _write_executable(
        app_dir / "bogo_ctl.sh",
        """
        #!/usr/bin/env bash
        exit 4
        """,
    )

    proc = subprocess.run(
        ["bash", str(app_dir / "bogo_oneclick.sh"), "start"],
        cwd=app_dir,
        env={**os.environ, "BOGO_MM_WAIT_TIMEOUT": "1"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "systemd --user is not reachable in this session" in proc.stderr


def test_oneclick_verifies_linux_bot_systemd_services_after_install(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    shutil.copytree(APP_DIR, app_dir, ignore=shutil.ignore_patterns(".venv", "logs", "__pycache__"))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    venv_bin = app_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_executable(
        venv_bin / "python",
        """
        #!/usr/bin/env bash
        if [ "${1:-}" = "-" ]; then
          exit 0
        fi
        case "${1:-}" in
          *net_autodetect.py)
            if [ "${2:-}" = "detect" ]; then
              echo '{"mode":"loopback"}'
            else
              echo "네트워크 자동 감지: 루프백"
            fi
            exit 0 ;;
          *)
            exit 0 ;;
        esac
        """,
    )
    _write_linux_uname(fake_bin)
    _write_executable(
        fake_bin / "systemctl",
        """
        #!/usr/bin/env bash
        if [ "${1:-}" = "--user" ] && [ "${2:-}" = "show-environment" ]; then
          exit 0
        fi
        if [ "${1:-}" = "--user" ] && [ "${2:-}" = "is-active" ]; then
          echo active
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        app_dir / "infra_up.sh",
        """
        #!/usr/bin/env bash
        exit 0
        """,
    )
    _write_executable(
        app_dir / "bogo_ctl.sh",
        """
        #!/usr/bin/env bash
        exit 0
        """,
    )
    _write_executable(
        app_dir / "migration" / "bogo_restore.sh",
        """
        #!/usr/bin/env bash
        exit 0
        """,
    )

    proc = subprocess.run(
        ["bash", str(app_dir / "bogo_oneclick.sh"), "start"],
        cwd=app_dir,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "BOGO_MM_WAIT_TIMEOUT": "1",
            "BOGO_DASH_WAIT_TIMEOUT": "1",
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "Verifying agent bot systemd services are active" in output
    assert "Agent bot systemd services active." in output


def test_linux_launcher_prints_compose_required_next_action_for_rc3(tmp_path: Path) -> None:
    root = tmp_path / "bogo"
    launcher_dir = root / "launchers"
    app_dir = root / "app"
    launcher_dir.mkdir(parents=True)
    app_dir.mkdir()
    shutil.copy2(APP_DIR.parent / "launchers" / "BOGO_start.sh", launcher_dir / "BOGO_start.sh")
    _write_executable(
        app_dir / "bogo_oneclick.sh",
        r"""
        #!/usr/bin/env bash
        echo "fake oneclick compose missing"
        exit 3
        """,
    )

    proc = subprocess.run(
        ["bash", str(launcher_dir / "BOGO_start.sh")],
        cwd=root,
        env={**os.environ, "BOGO_IN_TERM": "1"},
        input="",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 3, output
    assert "Docker Compose was explicitly required" in output
    assert "unset BOGO_REQUIRE_COMPOSE" in output
    assert "falls back to plain Docker CLI" in output
    assert "docker compose version" in output


def test_linux_launcher_prints_user_systemd_specific_next_action_for_rc4(tmp_path: Path) -> None:
    root = tmp_path / "bogo"
    launcher_dir = root / "launchers"
    app_dir = root / "app"
    launcher_dir.mkdir(parents=True)
    app_dir.mkdir()
    shutil.copy2(APP_DIR.parent / "launchers" / "BOGO_start.sh", launcher_dir / "BOGO_start.sh")
    _write_executable(
        app_dir / "bogo_oneclick.sh",
        """
        #!/usr/bin/env bash
        exit 4
        """,
    )

    proc = subprocess.run(
        ["bash", str(launcher_dir / "BOGO_start.sh")],
        cwd=root,
        env={**os.environ, "BOGO_IN_TERM": "1"},
        input="",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 4, output
    assert "Linux user services are not reachable" in output
    assert "systemctl --user status" in output


def test_linux_launcher_self_updates_and_reexecs_before_oneclick(tmp_path: Path) -> None:
    root = tmp_path / "bogo"
    launcher_dir = root / "launchers"
    app_dir = root / "app"
    fake_bin = tmp_path / "bin"
    git_state = tmp_path / "git-state"
    launcher_dir.mkdir(parents=True)
    app_dir.mkdir()
    fake_bin.mkdir()
    git_state.mkdir()
    shutil.copy2(APP_DIR.parent / "launchers" / "BOGO_start.sh", launcher_dir / "BOGO_start.sh")

    oneclick_calls = tmp_path / "oneclick.calls"
    git_calls = tmp_path / "git.calls"
    _write_executable(
        app_dir / "bogo_oneclick.sh",
        f"""
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {str(oneclick_calls)!r}
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "git",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [ "${1:-}" = "-C" ]; then
          shift 2
        fi
        printf '%s\n' "$*" >> "$FAKE_GIT_CALLS"

        case "${1:-}" in
          rev-parse)
            if [ "${2:-}" = "--is-inside-work-tree" ]; then
              echo true
              exit 0
            fi
            if [ "${2:-}" = "--abbrev-ref" ]; then
              echo origin/kh
              exit 0
            fi
            if [ "${2:-}" = "HEAD" ]; then
              count_file="$FAKE_GIT_STATE/head-count"
              count=0
              [ -f "$count_file" ] && count="$(cat "$count_file")"
              if [ "$count" = "0" ]; then
                echo 1111111111111111111111111111111111111111
              else
                echo 2222222222222222222222222222222222222222
              fi
              echo $((count + 1)) > "$count_file"
              exit 0
            fi
            ;;
          status)
            exit 0
            ;;
          fetch|merge)
            exit 0
            ;;
        esac

        echo "unexpected git args: $*" >&2
        exit 2
        """,
    )

    proc = subprocess.run(
        ["bash", str(launcher_dir / "BOGO_start.sh")],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "BOGO_IN_TERM": "1",
            "FAKE_GIT_CALLS": str(git_calls),
            "FAKE_GIT_STATE": str(git_state),
        },
        input="",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "Updated launcher code (1111111 -> 2222222); restarting with the latest version." in output
    assert oneclick_calls.read_text(encoding="utf-8").splitlines() == ["start"]
    call_log = git_calls.read_text(encoding="utf-8")
    assert "fetch --prune" in call_log
    assert "merge --ff-only origin/kh" in call_log


def test_linux_launcher_skips_self_update_when_tracked_files_are_dirty(tmp_path: Path) -> None:
    root = tmp_path / "bogo"
    launcher_dir = root / "launchers"
    app_dir = root / "app"
    fake_bin = tmp_path / "bin"
    launcher_dir.mkdir(parents=True)
    app_dir.mkdir()
    fake_bin.mkdir()
    shutil.copy2(APP_DIR.parent / "launchers" / "BOGO_start.sh", launcher_dir / "BOGO_start.sh")

    oneclick_calls = tmp_path / "oneclick.calls"
    git_calls = tmp_path / "git.calls"
    _write_executable(
        app_dir / "bogo_oneclick.sh",
        f"""
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {str(oneclick_calls)!r}
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "git",
        """
        #!/usr/bin/env bash
        set -euo pipefail
        if [ "${1:-}" = "-C" ]; then
          shift 2
        fi
        printf '%s\n' "$*" >> "$FAKE_GIT_CALLS"

        case "${1:-}" in
          rev-parse)
            if [ "${2:-}" = "--is-inside-work-tree" ]; then
              echo true
              exit 0
            fi
            if [ "${2:-}" = "--abbrev-ref" ]; then
              echo origin/kh
              exit 0
            fi
            ;;
          status)
            echo " M launchers/BOGO_start.sh"
            exit 0
            ;;
        esac

        echo "unexpected git args: $*" >&2
        exit 2
        """,
    )

    proc = subprocess.run(
        ["bash", str(launcher_dir / "BOGO_start.sh")],
        cwd=root,
        env={
            **os.environ,
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            "BOGO_IN_TERM": "1",
            "FAKE_GIT_CALLS": str(git_calls),
        },
        input="",
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "Tracked local changes exist" in output
    assert oneclick_calls.read_text(encoding="utf-8").splitlines() == ["start"]
    call_log = git_calls.read_text(encoding="utf-8")
    assert "fetch --prune" not in call_log
    assert "merge --ff-only origin/kh" not in call_log


def test_linux_systemd_install_fails_cleanly_when_systemctl_is_absent(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "uname",
        """
        #!/bin/bash
        echo Linux
        """,
    )
    _write_executable(
        fake_bin / "dirname",
        """
        #!/bin/bash
        case "$1" in
          */*) printf '%s\n' "${1%/*}" ;;
          *) printf '.\n' ;;
        esac
        """,
    )

    proc = subprocess.run(
        ["/bin/bash", str(APP_DIR / "service" / "install_service.sh"), "install"],
        cwd=APP_DIR,
        env={
            **os.environ,
            "PATH": str(fake_bin),
            "HOME": str(tmp_path / "home"),
        },
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert proc.returncode == 4, proc.stdout + proc.stderr
    assert "systemctl not found" in proc.stderr
    assert "not a systemd environment" in proc.stderr


def test_linux_systemd_templates_quote_repo_paths() -> None:
    # 계약 정정(신선 systemd 실측): WorkingDirectory= 는 '따옴표 없이' __WORKDIR__ 여야 한다.
    #   과거 이 테스트는 WorkingDirectory="__WORKDIR__"(따옴표)를 요구했으나, 실제 systemd 는
    #   선행 따옴표를 만나면 "path is not absolute" 로 유닛을 bad-setting 처리해 전 봇/대시보드
    #   기동이 실패했다(=버그를 코드화한 테스트였다). ExecStart= 는 셸형 파싱이라 경로 인자
    #   따옴표가 올바른 문법이므로 그대로 유지한다(둘의 systemd 파싱 규칙이 다르다).
    unit = (APP_DIR / "service" / "templates" / "bogo@.service.template").read_text(encoding="utf-8")
    backup = (APP_DIR / "service" / "templates" / "bogo-backup.service.template").read_text(encoding="utf-8")

    assert "WorkingDirectory=__WORKDIR__" in unit
    assert 'WorkingDirectory="__WORKDIR__"' not in unit
    assert 'ExecStart=/usr/bin/env bash "__WORKDIR__/run_role.sh" %i' in unit
    assert "NoNewPrivileges=true" in unit
    assert "PrivateTmp=true" in unit
    assert "WorkingDirectory=__WORKDIR__" in backup
    assert 'WorkingDirectory="__WORKDIR__"' not in backup
    assert 'ExecStart=/usr/bin/env bash "__WORKDIR__/migration/bogo_backup.sh"' in backup
    assert '--out "__WORKDIR__/migration"' in backup
    assert "NoNewPrivileges=true" in backup
    assert "PrivateTmp=true" in backup


def test_env_example_quotes_multihome_cors_value() -> None:
    example = (APP_DIR / ".env.example").read_text(encoding="utf-8")

    assert '# MM_ALLOW_CORS_FROM="http://172.16.0.10:8065 http://192.168.50.10:8065"' in example


def test_bootstrap_reuses_usable_venv_when_requirements_are_unchanged(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bootstrap.sh", app_dir / "bootstrap.sh")
    requirements = app_dir / "requirements.txt"
    requirements.write_text("demo-package==1\n", encoding="utf-8")
    env_file = app_dir / ".env"
    env_file.write_text("OPENROUTER_API_KEY=secret\n", encoding="utf-8")
    config_file = app_dir / "nk_config.json"
    config_file.write_text('{"bot_token":"secret"}\n', encoding="utf-8")
    env_file.chmod(0o644)
    config_file.chmod(0o644)
    venv_bin = app_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    calls = tmp_path / "venv-python.calls"
    _write_executable(
        venv_bin / "python",
        f"""
        #!/usr/bin/env bash
        printf '%s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          exit 0
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "pip" ]; then
          echo "pip should not run when requirements stamp matches" >&2
          exit 99
        fi
        exit 0
        """,
    )
    req_hash = hashlib.sha256(requirements.read_bytes()).hexdigest()
    (app_dir / ".venv" / ".bogo_requirements.sha256").write_text(f"{req_hash}\n", encoding="utf-8")

    proc = subprocess.run(
        ["bash", str(app_dir / "bootstrap.sh")],
        cwd=app_dir,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "Existing .venv is usable" in output
    assert "Requirements unchanged" in output
    call_log = calls.read_text(encoding="utf-8")
    assert "-m pip" not in call_log
    assert env_file.stat().st_mode & 0o077 == 0
    assert config_file.stat().st_mode & 0o077 == 0


def _selected_hermes_agent_versions(python_version: str) -> list[str]:
    env = default_environment()
    env["python_version"] = python_version
    selected = []
    for raw_line in (APP_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or not line.startswith("hermes-agent"):
            continue
        requirement = Requirement(line)
        if requirement.marker is None or requirement.marker.evaluate(env):
            selected.append(str(requirement.specifier))
    return selected


def test_requirements_select_python_specific_hermes_agent_versions() -> None:
    assert _selected_hermes_agent_versions("3.10") == ["<0.16,>=0.15.2"]
    assert _selected_hermes_agent_versions("3.11") == [">=0.17.0"]
    assert _selected_hermes_agent_versions("3.13") == [">=0.17.0"]
    assert _selected_hermes_agent_versions("3.9") == []
    assert _selected_hermes_agent_versions("3.14") == [">=0.17.0"]
    assert _selected_hermes_agent_versions("3.15") == [">=0.17.0"]
    assert _selected_hermes_agent_versions("3.99") == [">=0.17.0"]


def test_bootstrap_skips_too_old_python_candidates_before_venv(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bootstrap.sh", app_dir / "bootstrap.sh")
    (app_dir / "requirements.txt").write_text("hermes-agent==0.15.2\n", encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "python3",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          case "${2:-}" in
            *'sys.version_info[:3]'*) printf '3.9.18\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'too_old\n'; exit 0 ;;
          esac
        fi
        printf 'venv should not be created for unsupported Python\n' >&2
        exit 99
        """,
    )
    for name in ["python", "python3.13", "python3.12", "python3.11", "python3.10"]:
        _write_executable(
            fake_bin / name,
            r"""
            #!/usr/bin/env bash
            if [ "${1:-}" = "-c" ]; then
              case "${2:-}" in
                *'sys.version_info[:3]'*) printf '3.9.18\n'; exit 0 ;;
                *'v=sys.version_info'*) printf 'too_old\n'; exit 0 ;;
              esac
            fi
            printf 'venv should not be created for unsupported Python\n' >&2
            exit 99
            """,
        )

    proc = subprocess.run(
        ["bash", str(app_dir / "bootstrap.sh")],
        cwd=app_dir,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "Skipping Python 3.9.18" in output
    assert "Could not find a usable Python interpreter" not in output
    assert "No available Python candidate could create a working BOGO venv" in output
    assert "venv should not be created" not in output


def test_bootstrap_any_future_python_falls_back_when_hermes_is_missing(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bootstrap.sh", app_dir / "bootstrap.sh")
    shutil.copy2(APP_DIR / "requirements.txt", app_dir / "requirements.txt")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "python.calls"
    venv_py_future = tmp_path / "venv-python-future"
    venv_py_313 = tmp_path / "venv-python-313"
    _write_executable(
        venv_py_future,
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          printf '3.99.4\n'
          exit 0
        fi
        if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
          exit 0
        fi
        if [ "${1:-}" = "-" ]; then
          printf 'missing runtime dependency: hermes-agent\n' >&2
          exit 1
        fi
        exit 0
        """,
    )
    _write_executable(
        venv_py_313,
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          printf '3.13.5\n'
          exit 0
        fi
        if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
          exit 0
        fi
        if [ "${1:-}" = "-" ]; then
          exit 0
        fi
        exit 0
        """,
    )

    _write_executable(
        fake_bin / "python3",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'python3 %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          case "${{2:-}}" in
            *'sys.version_info[:3]'*) printf '3.99.4\\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'future\\n'; exit 0 ;;
          esac
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
          venv_dir="${{4:?}}"
          mkdir -p "$venv_dir/bin"
          cp {str(venv_py_future)!r} "$venv_dir/bin/python"
          chmod +x "$venv_dir/bin/python"
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "python",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          case "${2:-}" in
            *'sys.version_info[:3]'*) printf '3.9.18\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'too_old\n'; exit 0 ;;
          esac
        fi
        exit 99
        """,
    )
    _write_executable(
        fake_bin / "python3.13",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'python3.13 %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          case "${{2:-}}" in
            *'sys.version_info[:3]'*) printf '3.13.5\\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'supported\\n'; exit 0 ;;
          esac
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
          venv_dir="${{4:?}}"
          mkdir -p "$venv_dir/bin"
          cp {str(venv_py_313)!r} "$venv_dir/bin/python"
          chmod +x "$venv_dir/bin/python"
          exit 0
        fi
        exit 0
        """,
    )
    for name in ["python3.12", "python3.11", "python3.10"]:
        _write_executable(
            fake_bin / name,
            r"""
            #!/usr/bin/env bash
            exit 99
            """,
        )

    proc = subprocess.run(
        ["bash", str(app_dir / "bootstrap.sh")],
        cwd=app_dir,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "Trying future Python 3.99.4" in output
    assert "Runtime dependency validation failed after pip install" in output
    assert "Python 3.99.4" in output
    assert "could not satisfy BOGO dependencies; trying another interpreter" in output
    assert "Using Python:" in output
    assert "3.13.5" in output
    call_log = calls.read_text(encoding="utf-8")
    assert "python3 -m venv --copies" in call_log
    assert "python3.13 -m venv --copies" in call_log


def test_bootstrap_uses_virtualenv_helper_when_supported_python_ensurepip_fails(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bootstrap.sh", app_dir / "bootstrap.sh")
    shutil.copy2(APP_DIR / "requirements.txt", app_dir / "requirements.txt")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "python.calls"
    venv_py_future = tmp_path / "venv-python-future"
    helper_py = tmp_path / "helper-python"
    venv_py_311 = tmp_path / "venv-python-311"

    _write_executable(
        venv_py_future,
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          printf '3.99.4\n'
          exit 0
        fi
        if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
          case "${3:-}" in
            install)
              if [ "${4:-}" = "--upgrade" ]; then
                exit 0
              fi
              printf 'ERROR: No matching distribution found for hermes-agent>=0.17.0\n' >&2
              exit 1 ;;
          esac
        fi
        if [ "${1:-}" = "-" ]; then
          printf 'missing runtime dependency: hermes-agent\n' >&2
          exit 1
        fi
        exit 0
        """,
    )
    _write_executable(
        venv_py_311,
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          printf '3.11.15\n'
          exit 0
        fi
        if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
          exit 0
        fi
        if [ "${1:-}" = "-" ]; then
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        helper_py,
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'helper %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "pip" ]; then
          exit 0
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "virtualenv" ]; then
          if [ "${{3:-}}" = "--version" ]; then
            printf 'virtualenv 20.0.0\\n'
            exit 0
          fi
          dest="${{@: -1}}"
          mkdir -p "$dest/bin"
          cp {str(venv_py_311)!r} "$dest/bin/python"
          chmod +x "$dest/bin/python"
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "python3",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'python3 %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          case "${{2:-}}" in
            *'sys.version_info[:3]'*) printf '3.99.4\\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'future\\n'; exit 0 ;;
          esac
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
          venv_dir="${{4:?}}"
          mkdir -p "$venv_dir/bin"
          if [[ "$venv_dir" == *".bogo-bootstrap" ]]; then
            cp {str(helper_py)!r} "$venv_dir/bin/python"
          else
            cp {str(venv_py_future)!r} "$venv_dir/bin/python"
          fi
          chmod +x "$venv_dir/bin/python"
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "python",
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          case "${2:-}" in
            *'sys.version_info[:3]'*) printf '3.9.18\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'too_old\n'; exit 0 ;;
          esac
        fi
        exit 99
        """,
    )
    _write_executable(
        fake_bin / "python3.11",
        f"""
        #!/usr/bin/env bash
        printf 'python3.11 %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          case "${{2:-}}" in
            *'sys.version_info[:3]'*) printf '3.11.15\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'supported\n'; exit 0 ;;
          esac
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
          printf 'ensurepip returned non-zero exit status 1\n' >&2
          exit 1
        fi
        exit 0
        """,
    )
    for name in ["python3.13", "python3.12", "python3.10"]:
        _write_executable(
            fake_bin / name,
            r"""
            #!/usr/bin/env bash
            exit 99
            """,
        )

    proc = subprocess.run(
        ["bash", str(app_dir / "bootstrap.sh")],
        cwd=app_dir,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "Trying future Python 3.99.4" in output
    assert "Python 3.99.4" in output
    assert "could not satisfy BOGO dependencies; trying another interpreter" in output
    assert "Trying Python:" in output
    assert "3.11.15" in output
    assert "could not seed pip via venv; trying virtualenv helper" in output
    assert "Preparing virtualenv helper with Python:" in output
    assert "Created .venv for Python 3.11.15 via virtualenv helper" in output
    call_log = calls.read_text(encoding="utf-8")
    assert "python3.11 -m venv --copies" in call_log
    assert "helper -m pip install --upgrade pip virtualenv" in call_log
    assert "helper -m virtualenv --clear --copies -p" in call_log


def test_bootstrap_recovers_when_supported_python_venv_ensurepip_needs_without_pip(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bootstrap.sh", app_dir / "bootstrap.sh")
    shutil.copy2(APP_DIR / "requirements.txt", app_dir / "requirements.txt")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    calls = tmp_path / "python.calls"
    pip_seeded = tmp_path / "pip-seeded"
    venv_py_future = tmp_path / "venv-python-314"
    venv_py_311 = tmp_path / "venv-python-311"

    _write_executable(
        venv_py_future,
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          printf '3.14.4\n'
          exit 0
        fi
        if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
          if [ "${3:-}" = "install" ] && [ "${4:-}" = "-r" ]; then
            printf 'ERROR: Package hermes-agent requires Python <3.14\n' >&2
            exit 1
          fi
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        venv_py_311,
        f"""
        #!/usr/bin/env bash
        printf 'venv311 %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          printf '3.11.15\\n'
          exit 0
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "ensurepip" ]; then
          touch {str(pip_seeded)!r}
          exit 0
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "pip" ]; then
          if [ ! -f {str(pip_seeded)!r} ]; then
            exit 1
          fi
          exit 0
        fi
        if [ "${{1:-}}" = "-" ]; then
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "python3",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'python3 %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          case "${{2:-}}" in
            *'sys.version_info[:3]'*) printf '3.14.4\\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'future\\n'; exit 0 ;;
          esac
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
          venv_dir="${{@: -1}}"
          mkdir -p "$venv_dir/bin"
          cp {str(venv_py_future)!r} "$venv_dir/bin/python"
          chmod +x "$venv_dir/bin/python"
          exit 0
        fi
        exit 0
        """,
    )
    for name in ["python", "python3.13", "python3.12", "python3.10"]:
        _write_executable(
            fake_bin / name,
            r"""
            #!/usr/bin/env bash
            if [ "${1:-}" = "-c" ]; then
              case "${2:-}" in
                *'sys.version_info[:3]'*) printf '3.9.18\n'; exit 0 ;;
                *'v=sys.version_info'*) printf 'too_old\n'; exit 0 ;;
              esac
            fi
            exit 99
            """,
        )
    _write_executable(
        fake_bin / "python3.11",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        printf 'python3.11 %s\\n' "$*" >> {str(calls)!r}
        if [ "${{1:-}}" = "-c" ]; then
          case "${{2:-}}" in
            *'sys.version_info[:3]'*) printf '3.11.15\\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'supported\\n'; exit 0 ;;
          esac
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
          venv_dir="${{@: -1}}"
          if [ "${{4:-}}" = "--without-pip" ]; then
            mkdir -p "$venv_dir/bin"
            cp {str(venv_py_311)!r} "$venv_dir/bin/python"
            chmod +x "$venv_dir/bin/python"
            exit 0
          fi
          printf 'ensurepip returned non-zero exit status 1\\n' >&2
          exit 1
        fi
        exit 0
        """,
    )

    proc = subprocess.run(
        ["bash", str(app_dir / "bootstrap.sh")],
        cwd=app_dir,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, output
    assert "Trying future Python 3.14.4" in output
    assert "Dependency installation failed" in output
    assert "Python 3.14.4" in output
    assert "could not satisfy BOGO dependencies; trying another interpreter" in output
    assert "Trying Python:" in output
    assert "3.11.15" in output
    assert "Created .venv without pip; trying to seed pip" in output
    call_log = calls.read_text(encoding="utf-8")
    assert "python3 -m venv --copies" in call_log
    assert "python3.11 -m venv --copies" in call_log
    assert "python3.11 -m venv --copies --without-pip" in call_log
    assert "venv311 -m ensurepip --upgrade" in call_log


def test_bootstrap_pip_failure_reports_python_and_hermes_marker_contract(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bootstrap.sh", app_dir / "bootstrap.sh")
    shutil.copy2(APP_DIR / "requirements.txt", app_dir / "requirements.txt")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    venv_py_310 = tmp_path / "venv-python-310"
    _write_executable(
        venv_py_310,
        r"""
        #!/usr/bin/env bash
        if [ "${1:-}" = "-c" ]; then
          printf '3.10.12\n'
          exit 0
        fi
        if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ]; then
          if [ "${3:-}" = "--version" ]; then
            exit 0
          fi
          if [ "${3:-}" = "install" ] && [ "${4:-}" = "--upgrade" ]; then
            exit 0
          fi
          printf 'ERROR: Could not find a version that satisfies the requirement hermes-agent==0.17.0\n' >&2
          exit 1
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "python3",
        f"""
        #!/usr/bin/env bash
        set -euo pipefail
        if [ "${{1:-}}" = "-c" ]; then
          case "${{2:-}}" in
            *'sys.version_info[:3]'*) printf '3.10.12\n'; exit 0 ;;
            *'v=sys.version_info'*) printf 'supported\n'; exit 0 ;;
          esac
        fi
        if [ "${{1:-}}" = "-m" ] && [ "${{2:-}}" = "venv" ]; then
          venv_dir="${{4:?}}"
          mkdir -p "$venv_dir/bin"
          cp {str(venv_py_310)!r} "$venv_dir/bin/python"
          chmod +x "$venv_dir/bin/python"
          exit 0
        fi
        exit 0
        """,
    )

    proc = subprocess.run(
        ["bash", str(app_dir / "bootstrap.sh")],
        cwd=app_dir,
        env={**os.environ, "PATH": f"{fake_bin}{os.pathsep}/usr/bin:/bin", "BOGO_PYTHON": str(fake_bin / "python3")},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "Dependency installation failed" in output
    assert "Python in use: 3.10.12" in output
    assert "Python 3.10 -> 0.15.x" in output
    assert "Python 3.11+ -> latest compatible hermes-agent from PyPI" in output
    assert "sudo apt install python3 python3-venv" not in output


def test_oneclick_preserves_bootstrap_pip_failure_without_venv_relabel(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bogo_oneclick.sh", app_dir / "bogo_oneclick.sh")
    _write_executable(
        app_dir / "bootstrap.sh",
        r"""
        #!/usr/bin/env bash
        printf '[bootstrap:error] Dependency installation failed.\n' >&2
        printf '[bootstrap:error] Python in use: 3.10.12\n' >&2
        exit 1
        """,
    )

    proc = subprocess.run(
        ["bash", str(app_dir / "bogo_oneclick.sh"), "start"],
        cwd=app_dir,
        env={**os.environ, "PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "Dependency installation failed" in output
    assert "see the [bootstrap:error] line above" in output
    assert "Python/pip package compatibility" in output
    assert "sudo apt install python3 python3-venv" not in output
    assert "Check whether Python 3 + venv are installed" not in output


def test_oneclick_bootstrap_failure_keeps_real_dependency_error(tmp_path: Path) -> None:
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    shutil.copy2(APP_DIR / "bogo_oneclick.sh", app_dir / "bogo_oneclick.sh")
    venv_bin = app_dir / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    _write_executable(
        venv_bin / "python",
        """
        #!/usr/bin/env bash
        exit 1
        """,
    )
    _write_executable(
        app_dir / "bootstrap.sh",
        """
        #!/usr/bin/env bash
        echo "[bootstrap:error] Dependency installation failed." >&2
        exit 1
        """,
    )

    proc = subprocess.run(
        ["bash", str(app_dir / "bogo_oneclick.sh"), "start"],
        cwd=app_dir,
        env={**os.environ, "BOGO_MM_WAIT_TIMEOUT": "1"},
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    output = proc.stdout + proc.stderr
    assert proc.returncode == 1, output
    assert "Dependency installation failed." in output
    assert "see the [bootstrap:error] line above" in output
    assert "Python/pip package compatibility" in output
    assert "sudo apt install python3 python3-venv" not in output
