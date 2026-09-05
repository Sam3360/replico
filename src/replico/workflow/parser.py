"""GitHub Actions workflow parsing.

Two layers:

1. :func:`materialize_yaml` — a bomb-safe YAML engine. PyYAML's ``safe_load``
   defers construction, so documents whose anchors alias other anchors can
   expand *exponentially* the moment the result is traversed. Instead we
   compose the document to a node tree (cheap, no expansion) and then
   materialize it ourselves with per-node memoization, a hard node budget
   and a depth limit. Alias bombs therefore cost O(unique nodes).

2. :func:`parse_workflow` — converts the safe plain structure into the typed
   :mod:`replico.models` objects (jobs, steps, matrices, env).
"""

from __future__ import annotations

import base64
import io
import re
from typing import Any

import yaml

from replico.errors import InvalidInputError, UnsupportedError
from replico.models import MatrixSpec, Workflow, WorkflowJob, WorkflowStep

MAX_INPUT_BYTES = 2 * 1024 * 1024
MAX_NODES = 100_000
MAX_DEPTH = 400
MAX_ROUGH_NESTING = 600

_BRACKET_DEPTH_RE = re.compile(r"[\[{(]")

TAG_PREFIX = "tag:yaml.org,2002:"


def _rough_nesting_scan(text: str) -> int:
    """Cheap linear estimate of flow/bracket nesting to reject bombs early."""
    depth = 0
    max_depth = 0
    for char in text:
        if char in "[{(":
            depth += 1
            max_depth = max(max_depth, depth)
        elif char in "]})":
            depth = max(0, depth - 1)
        if max_depth > MAX_ROUGH_NESTING:
            return max_depth
    return max_depth


class _BudgetExceeded(Exception):
    pass


class _TooDeep(Exception):
    pass


def _compose(text: str) -> yaml.nodes.Node:
    try:
        loader = yaml.SafeLoader(io.StringIO(text))
    except yaml.YAMLError as exc:
        raise InvalidInputError(f"workflow YAML could not be read: {exc}") from exc
    try:
        node = loader.get_single_node()
        if node is None:
            raise InvalidInputError("workflow file is empty")
        return node
    except RecursionError as exc:
        raise InvalidInputError("workflow YAML is nested too deeply to parse safely") from exc
    except yaml.YAMLError as exc:
        raise InvalidInputError(f"malformed workflow YAML: {exc}") from exc
    finally:
        loader.dispose()


def _decode_scalar(node: yaml.nodes.ScalarNode) -> Any:
    """Decode a scalar node honoring its resolved YAML tag."""
    tag = node.tag
    value = node.value
    if tag == f"{TAG_PREFIX}null":
        return None
    if tag == f"{TAG_PREFIX}bool":
        return value.strip().lower() in ("true", "yes", "on")
    if tag == f"{TAG_PREFIX}int":
        try:
            lowered = value.lower().replace("_", "")
            if lowered.startswith(("0x", "+0x", "-0x")):
                return int(lowered, 16)
            if lowered.startswith(("0o", "+0o", "-0o")):
                return int(lowered, 8)
            if lowered.startswith(("0b", "+0b", "-0b")):
                return int(lowered, 2)
            if lowered.startswith(("0", "+0", "-0")) and len(lowered) > 1:
                # YAML 1.1 octal like 0777
                try:
                    return int(lowered, 8)
                except ValueError:
                    return int(lowered)
            return int(lowered)
        except ValueError:
            return value
    if tag == f"{TAG_PREFIX}float":
        try:
            return float(value)
        except ValueError:
            return value
    if tag == f"{TAG_PREFIX}binary":
        try:
            return base64.b64decode(value).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return value
    # Everything else (str, timestamp, custom tags) is treated as plain text.
    return value


def materialize_yaml(text: str) -> Any:
    """Safe, budgeted YAML materialization (dict/list/scalar structure)."""
    if len(text.encode("utf-8")) > MAX_INPUT_BYTES:
        raise InvalidInputError(
            f"workflow YAML exceeds the {MAX_INPUT_BYTES // (1024 * 1024)} MiB safety limit"
        )
    if _rough_nesting_scan(text) > MAX_ROUGH_NESTING:
        raise InvalidInputError("workflow YAML flow-nesting is too deep to parse safely")

    root = _compose(text)
    cache: dict[int, Any] = {}
    state = {"count": 0}

    def build(node: yaml.nodes.Node, depth: int) -> Any:
        if depth > MAX_DEPTH:
            raise _TooDeep()
        state["count"] += 1
        if state["count"] > MAX_NODES:
            raise _BudgetExceeded()
        # PyYAML resolves aliases to the original node object, so the
        # id() cache below makes alias bombs cost O(unique nodes).
        key = id(node)
        if key in cache:
            return cache[key]
        if isinstance(node, yaml.nodes.ScalarNode):
            value = _decode_scalar(node)
            cache[key] = value
            return value
        if isinstance(node, yaml.nodes.SequenceNode):
            result: list[Any] = []
            cache[key] = result
            for child in node.value:
                result.append(build(child, depth + 1))
            return result
        if isinstance(node, yaml.nodes.MappingNode):
            result = _build_mapping(node, build, depth)
            cache[key] = result
            return result
        raise InvalidInputError(f"unsupported YAML node kind {node.__class__.__name__}")

    try:
        return build(root, 0)
    except _BudgetExceeded as exc:
        raise InvalidInputError(
            "workflow YAML contains too many nodes to process safely "
            "(possible alias-expansion attack)"
        ) from exc
    except _TooDeep as exc:
        raise InvalidInputError("workflow YAML is nested too deeply") from exc
    except RecursionError as exc:
        raise InvalidInputError("workflow YAML is nested too deeply to parse safely") from exc


def _build_mapping(node, build, depth):
    """Mapping constructor with YAML merge-key (``<<``) support."""
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = build(key_node, depth + 1)
        if key == "<<":
            merge_value = _resolve_merge(value_node, build, depth)
            for merged in merge_value:
                for mkey, mvalue in merged.items():
                    result.setdefault(mkey, mvalue)
            continue
        value = build(value_node, depth + 1)
        result[key] = value
    return result


def _resolve_merge(value_node, build, depth):
    if isinstance(value_node, yaml.nodes.SequenceNode):
        out = []
        for child in value_node.value:
            out.append(build(child, depth + 1))
        return out
    built = build(value_node, depth + 1)
    if isinstance(built, dict):
        return [built]
    return []


# --------------------------------------------------------------------------
# Typed workflow model
# --------------------------------------------------------------------------

_KNOWN_ACTIONS = {
    "actions/checkout",
    "actions/setup-python",
    "actions/setup-node",
    "actions/setup-java",
    "actions/setup-go",
    "actions/setup-dotnet",
}


def _scalar_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _stringify_mapping(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {_scalar_to_text(k): _scalar_to_text(v) for k, v in value.items()}


def _parse_env(value: Any) -> dict[str, str]:
    """env: may be a mapping or (Windows style) a list of mappings."""
    if isinstance(value, dict):
        return _stringify_mapping(value)
    if isinstance(value, list):
        merged: dict[str, str] = {}
        for entry in value:
            if isinstance(entry, dict):
                merged.update(_stringify_mapping(entry))
        return merged
    return {}


def _parse_matrix(value: Any) -> MatrixSpec | None:
    if not isinstance(value, dict):
        return None
    axes: list[tuple[str, list]] = []
    for key, raw_values in value.items():
        if key in ("include", "exclude", "name"):
            continue
        if isinstance(raw_values, list):
            axes.append((_scalar_to_text(key), [_scalar_to_text(v) for v in raw_values]))
    include = [
        _stringify_mapping(entry)
        for entry in (value.get("include") or [])
        if isinstance(entry, dict)
    ]
    exclude = [
        _stringify_mapping(entry)
        for entry in (value.get("exclude") or [])
        if isinstance(entry, dict)
    ]
    name_template = _scalar_to_text(value.get("name")) if value.get("name") else None
    return MatrixSpec(axes=axes, include=include, exclude=exclude, name_template=name_template)


def _parse_steps(raw_steps: Any) -> list[WorkflowStep]:
    if not isinstance(raw_steps, list):
        return []
    steps: list[WorkflowStep] = []
    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            continue
        step = WorkflowStep(
            number=index,
            name=raw.get("name") if isinstance(raw.get("name"), str) else None,
            uses=raw.get("uses") if isinstance(raw.get("uses"), str) else None,
            run=raw.get("run") if isinstance(raw.get("run"), str) else None,
            with_args=_stringify_mapping(raw.get("with")),
            env=_parse_env(raw.get("env")),
            working_directory=(
                raw.get("working-directory")
                if isinstance(raw.get("working-directory"), str)
                else None
            ),
            shell=raw.get("shell") if isinstance(raw.get("shell"), str) else None,
            step_id=raw.get("id") if isinstance(raw.get("id"), str) else None,
            if_condition=raw.get("if") if isinstance(raw.get("if"), str) else None,
        )
        if step.name is None:
            step.name = step.display_name  # materialize a sensible name early
        steps.append(step)
    return steps


def parse_workflow(text: str, source_path: str = ".github/workflows/workflow.yml") -> Workflow:
    """Parse workflow YAML text into a :class:`Workflow`."""
    data = materialize_yaml(text)
    if not isinstance(data, dict):
        raise InvalidInputError(
            f"workflow file {source_path} must contain a YAML mapping at the top level"
        )
    name = data.get("name")
    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, dict):
        raise UnsupportedError(
            f"workflow file {source_path} has no 'jobs' mapping — Replico cannot plan a reproduction"
        )
    jobs: dict[str, WorkflowJob] = {}
    for job_id, raw_job in jobs_raw.items():
        job_id = _scalar_to_text(job_id)
        if not isinstance(raw_job, dict):
            continue
        strategy = raw_job.get("strategy")
        defaults = raw_job.get("defaults")
        runs_on = raw_job.get("runs-on")
        if isinstance(runs_on, list):
            runs_on_list = [_scalar_to_text(entry) for entry in runs_on]
        elif runs_on is not None:
            runs_on_list = [_scalar_to_text(runs_on)]
        else:
            runs_on_list = []
        matrix = _parse_matrix(strategy.get("matrix")) if isinstance(strategy, dict) else None
        default_shell = None
        if isinstance(defaults, dict):
            run_defaults = defaults.get("run")
            if isinstance(run_defaults, dict) and isinstance(run_defaults.get("shell"), str):
                default_shell = run_defaults["shell"]
        needs = raw_job.get("needs")
        if isinstance(needs, str):
            needs_list = [needs]
        elif isinstance(needs, list):
            needs_list = [_scalar_to_text(n) for n in needs]
        else:
            needs_list = []
        timeout = raw_job.get("timeout-minutes")
        jobs[job_id] = WorkflowJob(
            id=job_id,
            name=raw_job.get("name") if isinstance(raw_job.get("name"), str) else None,
            runs_on=runs_on_list,
            env=_parse_env(raw_job.get("env")),
            steps=_parse_steps(raw_job.get("steps")),
            matrix=matrix,
            default_shell=default_shell,
            needs=needs_list,
            timeout_minutes=timeout if isinstance(timeout, int) else None,
        )
    return Workflow(
        path=source_path,
        name=name if isinstance(name, str) else None,
        jobs=jobs,
        raw_text=text,
    )


def uses_known_setup_action(step: WorkflowStep) -> bool:
    if not step.uses:
        return False
    action = step.uses.split("@")[0].strip()
    return action in _KNOWN_ACTIONS


def action_name(uses: str) -> str:
    return uses.split("@")[0].strip() if uses else ""
