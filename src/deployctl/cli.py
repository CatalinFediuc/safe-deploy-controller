import argparse
import os
import secrets
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any, NoReturn

from deployctl import __version__, render
from deployctl.audit import AuditLog
from deployctl.clock import Clock, SystemClock
from deployctl.controller import DeploymentController, DeployOptions, validate
from deployctl.errors import DeployctlError, HealthProfileError
from deployctl.health import FixtureHealthProbe
from deployctl.manifest import ENVIRONMENTS, SERVICE_RE
from deployctl.models import DeploymentResult, ExitCode
from deployctl.state import StateStore
from deployctl.target import SimulatedTarget

_DEFAULT_STATE_DIR = ".deployctl"
_DEFAULT_HISTORY_LIMIT = 20
_STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP)


def _new_run_id(clock: Clock) -> str:
    # Lock release relies on this being unique, and a time prefix keeps listings in order.
    return f"r-{clock.now():%Y%m%dT%H%M%S}-{secrets.token_hex(3)}"


def _controller(args: argparse.Namespace, clock: Clock) -> DeploymentController:
    """The one place adapters are chosen. The profile is read here, before any lock or state."""
    return DeploymentController(
        store=StateStore(args.state_dir, clock),
        target=SimulatedTarget(args.state_dir, clock),
        probe=FixtureHealthProbe(args.health_profile),
        audit=AuditLog(args.state_dir),
        clock=clock,
        run_id_factory=lambda: _new_run_id(clock),
    )


def _print_document(args: argparse.Namespace, document: dict[str, Any], text: str) -> None:
    print(render.to_json(document) if args.output == "json" else text)


def _finish(args: argparse.Namespace, result: DeploymentResult) -> int:
    _print_document(args, render.result_document(result), render.result_text(result))
    return int(result.exit_code)


def _fail(args: argparse.Namespace, message: str, code: ExitCode) -> int:
    print(f"error: {message}", file=sys.stderr)
    if args.output == "json":
        print(render.to_json({"error": message, "exit_code": int(code)}))
    return int(code)


def _validate(args: argparse.Namespace) -> int:
    clock = SystemClock()
    return _finish(args, validate(args.manifest, args.artifact_dir, clock, _new_run_id(clock)))


def _deploy(args: argparse.Namespace) -> int:
    opts = DeployOptions(
        dry_run=args.dry_run,
        allow_known_bad=args.allow_known_bad,
        allow_downgrade=args.allow_downgrade,
    )
    controller = _controller(args, SystemClock())
    return _finish(args, controller.deploy(args.manifest, args.artifact_dir, opts))


def _recover(args: argparse.Namespace) -> int:
    controller = _controller(args, SystemClock())
    result = controller.recover(
        args.service, args.env, args.reason, args.actor, break_lock=args.break_lock
    )
    return _finish(args, result)


def _status(args: argparse.Namespace) -> int:
    clock = SystemClock()
    state = StateStore(args.state_dir, clock).find(args.service, args.env)
    if state is None:
        message = f"no record of {args.service}/{args.env} in {args.state_dir}"
        return _fail(args, message, ExitCode.BLOCKED)
    running = SimulatedTarget(args.state_dir, clock).running_version(state.env_key)
    document = render.status_document(state, running)
    _print_document(args, document, render.status_text(document))
    return int(ExitCode.OK)


def _history(args: argparse.Namespace) -> int:
    history = AuditLog(args.state_dir).history(args.service, args.env, args.limit)
    if history.skipped_lines:
        print(f"warning: unreadable audit lines skipped: {history.skipped_lines}", file=sys.stderr)
    _print_document(
        args,
        render.history_document(args.service, args.env, history),
        render.history_text(args.service, args.env, history),
    )
    return int(ExitCode.OK)


def _service_name(value: str) -> str:
    if SERVICE_RE.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(f"{value!r} is not a valid service name")
    return value


def _not_blank(value: str) -> str:
    if not value.strip():
        raise argparse.ArgumentTypeError("must not be blank")
    return value


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"{value!r} is not a positive whole number")
    return number


def _add_environment(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--service", required=True, type=_service_name)
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)


def _add_manifest(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)


def _parser() -> argparse.ArgumentParser:
    output = argparse.ArgumentParser(add_help=False)
    output.add_argument("--output", choices=("text", "json"), default="text")
    state = argparse.ArgumentParser(add_help=False)
    state.add_argument(
        "--state-dir",
        type=Path,
        default=os.environ.get("DEPLOYCTL_STATE_DIR") or _DEFAULT_STATE_DIR,
    )

    parser = argparse.ArgumentParser(prog="deployctl", description="Safe deployment controller")
    parser.add_argument("--version", action="version", version=f"deployctl {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    validate_cmd = commands.add_parser("validate", parents=[output], help="check a manifest")
    _add_manifest(validate_cmd)
    validate_cmd.set_defaults(handler=_validate)

    deploy = commands.add_parser("deploy", parents=[output, state], help="roll out a manifest")
    _add_manifest(deploy)
    deploy.add_argument("--health-profile", required=True, type=Path)
    deploy.add_argument("--dry-run", action="store_true")
    deploy.add_argument("--allow-known-bad", action="store_true")
    deploy.add_argument("--allow-downgrade", action="store_true")
    deploy.set_defaults(handler=_deploy)

    recover = commands.add_parser(
        "recover", parents=[output, state], help="restore the last known-good version"
    )
    _add_environment(recover)
    recover.add_argument("--reason", required=True, type=_not_blank)
    recover.add_argument("--health-profile", required=True, type=Path)
    recover.add_argument("--break-lock", action="store_true")
    user = os.environ.get("USER") or None  # the audit needs a name, so no $USER means no default
    recover.add_argument("--actor", type=_not_blank, default=user, required=user is None)
    recover.set_defaults(handler=_recover)

    status = commands.add_parser("status", parents=[output, state], help="show one environment")
    _add_environment(status)
    status.set_defaults(handler=_status)

    history = commands.add_parser("history", parents=[output, state], help="list recent runs")
    # Not validated: rejected runs can be audited under invalid names such as `qa`.
    history.add_argument("--service", required=True)
    history.add_argument("--env", required=True)
    history.add_argument("--limit", type=_positive_int, default=_DEFAULT_HISTORY_LIMIT)
    history.set_defaults(handler=_history)
    return parser


def _raise_interrupt(signum: int, frame: FrameType | None) -> NoReturn:
    raise KeyboardInterrupt


@contextmanager
def _stop_signals_interrupt() -> Iterator[None]:
    """Left alone, these end the process with no `finally`, so the lock would stay behind."""
    previous = {number: signal.signal(number, _raise_interrupt) for number in _STOP_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    try:
        with _stop_signals_interrupt():
            return handler(args)
    except HealthProfileError as exc:
        return _fail(args, str(exc), ExitCode.USAGE)
    except DeployctlError as exc:  # StateCorruptError, RecoveryRequired, InvariantError
        return _fail(args, str(exc), ExitCode.INTERNAL_ERROR)
    except KeyboardInterrupt:  # outside a run: the controller handles the ones inside
        return _fail(args, "interrupted", ExitCode.INTERRUPTED)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}; run `deployctl status` to check for a stopped run"
        return _fail(args, message, ExitCode.INTERNAL_ERROR)


def run() -> NoReturn:  # pragma: no cover - thin shim, exercised by `make smoke`
    sys.exit(main())
