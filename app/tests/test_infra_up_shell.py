import os
import shutil
import socket
import subprocess
import textwrap
from pathlib import Path


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
    assert "Container creation/startup via compose complete." in proc.stdout
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


def test_infra_up_returns_compose_specific_code_when_compose_is_missing(tmp_path: Path) -> None:
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
        log="${FAKE_DOCKER_STATE_DIR}/calls.log"
        printf '%s\n' "$*" >> "$log"

        if [ "${1:-}" = "info" ]; then
          exit 0
        fi
        if [ "${1:-}" = "compose" ] && [ "${2:-}" = "version" ]; then
          exit 127
        fi
        if [ "${1:-}" = "inspect" ]; then
          echo absent
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

    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert "Docker Desktop/Compose plugin must be installed" in proc.stderr


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
        (3, "Cause: Docker Compose is not available."),
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


def test_linux_launcher_prints_compose_specific_next_action_for_rc3(tmp_path: Path) -> None:
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
    assert "Stopped because Docker Compose is not available." in output
    assert "install/enable Docker Compose" in output
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
    unit = (APP_DIR / "service" / "templates" / "bogo@.service.template").read_text(encoding="utf-8")
    backup = (APP_DIR / "service" / "templates" / "bogo-backup.service.template").read_text(encoding="utf-8")

    assert 'WorkingDirectory="__WORKDIR__"' in unit
    assert 'ExecStart=/usr/bin/env bash "__WORKDIR__/run_role.sh" %i' in unit
    assert 'WorkingDirectory="__WORKDIR__"' in backup
    assert 'ExecStart=/usr/bin/env bash "__WORKDIR__/migration/bogo_backup.sh"' in backup
    assert '--out "__WORKDIR__/migration"' in backup


def test_env_example_quotes_multihome_cors_value() -> None:
    example = (APP_DIR / ".env.example").read_text(encoding="utf-8")

    assert '# MM_ALLOW_CORS_FROM="http://172.16.0.10:8065 http://192.168.50.10:8065"' in example
