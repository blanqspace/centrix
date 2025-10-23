#!/usr/bin/env python3
"""Collect Centrix diagnostics into a portable bundle."""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import platform
import shutil
from collections import deque
from pathlib import Path
from typing import Iterator, Sequence


MAX_TREE_DEPTH = 4
RECENT_LOG_LINES = 200
SENSITIVE_KEY_MARKERS = (
    "TOKEN",
    "SECRET",
    "KEY",
    "PASS",
    "PWD",
    "SIGNING",
    "APP_",
    "XOXB",
    "XAPP",
)
ENV_FLAG_EXCLUDE_MARKERS = ("CHANNEL", "ROLE_MAP")
LOG_FILE_EXTENSIONS = (".log", ".out", ".err", ".txt")
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".mypy_cache", ".venv"}


def mask_value(value: str, visible: int = 6) -> str:
    """Mask value keeping the first `visible` characters."""
    if visible <= 0:
        return "*" * len(value)
    if len(value) <= visible:
        return value
    return value[:visible] + "*" * (len(value) - visible)


def mask_identifier(identifier: str, visible: int = 4) -> str:
    """Mask identifiers such as channel IDs or user IDs."""
    identifier = identifier.strip()
    if not identifier:
        return identifier
    return mask_value(identifier, visible)


def is_sensitive_key(key: str) -> bool:
    upper_key = key.upper()
    return any(marker in upper_key for marker in SENSITIVE_KEY_MARKERS)


def is_visible_env_flag(key: str) -> bool:
    upper_key = key.upper()
    if is_sensitive_key(key):
        return False
    return not any(marker in upper_key for marker in ENV_FLAG_EXCLUDE_MARKERS)


def build_tree(root: Path, max_depth: int = MAX_TREE_DEPTH) -> str:
    """Return an ASCII tree representation similar to `tree -L`."""
    lines = [root.name]

    def _iter_entries(directory: Path) -> Iterator[Path]:
        try:
            entries = sorted(directory.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return iter(())
        for entry in entries:
            yield entry

    def _walk(directory: Path, prefix: str, depth: int) -> None:
        if depth >= max_depth:
            return
        entries = [entry for entry in _iter_entries(directory)]
        for index, entry in enumerate(entries):
            connector = "└── " if index == len(entries) - 1 else "├── "
            lines.append(f"{prefix}{connector}{entry.name}")
            if entry.is_dir():
                extension = "    " if index == len(entries) - 1 else "│   "
                _walk(entry, prefix + extension, depth + 1)

    _walk(root, "", 0)
    return "\n".join(lines)


def list_files_within(directory: Path, root: Path) -> list[str]:
    """Return sorted list of file paths (relative to root) within directory."""
    results: list[str] = []
    for current_root, dirs, files in os.walk(directory):
        dirs[:] = sorted(d for d in dirs)
        rel_dir = Path(current_root).relative_to(root)
        for name in sorted(files):
            rel_path = rel_dir / name
            results.append(str(rel_path).replace(os.sep, "/"))
    return results


def count_files(directory: Path) -> int:
    total = 0
    for _, _, files in os.walk(directory):
        total += len(files)
    return total


def tail_file(path: Path, max_lines: int = RECENT_LOG_LINES) -> str:
    """Return the last `max_lines` of a text file."""
    try:
        with path.open("rb") as fh:
            dq: deque[bytes] = deque(maxlen=max_lines)
            for raw_line in fh:
                dq.append(raw_line)
    except OSError as exc:
        raise RuntimeError(f"Unable to read {path}: {exc}") from exc
    decoded_lines = [line.decode("utf-8", errors="replace") for line in dq]
    return "".join(decoded_lines)


def gather_recent_files(
    root: Path,
    limit: int = 10,
    exclude_dirs: Sequence[str] | None = None,
) -> list[tuple[str, float]]:
    exclude = set(exclude_dirs or ())
    results: list[tuple[str, float]] = []
    for current_root, dirs, files in os.walk(root):
        rel = Path(current_root).relative_to(root)
        dirs[:] = [
            d for d in dirs if d not in exclude and d not in SKIP_DIRS
        ]
        for name in files:
            path = Path(current_root) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            rel_path = str(path.relative_to(root)).replace(os.sep, "/")
            results.append((rel_path, stat.st_mtime))
    results.sort(key=lambda item: item[1], reverse=True)
    return results[:limit]


def read_env_file(env_path: Path) -> list[tuple[str, str, str]]:
    """Return list of (key, raw_value, original_line) tuples."""
    entries: list[tuple[str, str, str]] = []
    if not env_path.exists():
        return entries
    with env_path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                entries.append(("", "", line))
                continue
            key, _, value = line.partition("=")
            entries.append((key.strip(), value.rstrip("\n"), line))
    return entries


def sanitize_env(
    env_entries: list[tuple[str, str, str]],
    destination: Path,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Write sanitized env file and return key/value info and non-sensitive flag lines."""
    sanitized_pairs: list[tuple[str, str]] = []
    non_sensitive_lines: list[str] = []

    ensure_directory(destination.parent)
    with destination.open("w", encoding="utf-8") as dest:
        for key, value, original in env_entries:
            if not key:
                dest.write(original)
                continue
            newline = "\n" if original.endswith("\n") else ""
            sanitized_value = value
            if is_sensitive_key(key):
                sanitized_value = mask_value(value)
                dest.write(f"{key}={sanitized_value}{newline}")
            else:
                dest.write(f"{key}={value}{newline}")
                if value and is_visible_env_flag(key):
                    non_sensitive_lines.append(f"{key}={value}")
            sanitized_pairs.append((key, sanitized_value))
    return sanitized_pairs, non_sensitive_lines


def parse_role_map(raw_value: str) -> dict[str, str]:
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError:
        return {}
    if isinstance(parsed, dict):
        return {str(k): str(v) for k, v in parsed.items()}
    return {}


def ensure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8192), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect_logs(root: Path, destination: Path, errors: list[str]) -> list[Path]:
    created: list[Path] = []
    ensure_directory(destination)
    for current_root, dirs, files in os.walk(root):
        rel_dir = Path(current_root).relative_to(root)
        dirs[:] = [
            d for d in dirs if d not in SKIP_DIRS and not str((rel_dir / d)).startswith("diagnostics")
        ]
        for name in files:
            lower_name = name.lower()
            if not lower_name.endswith(LOG_FILE_EXTENSIONS):
                continue
            source = Path(current_root) / name
            relative_path = source.relative_to(root)
            target_dir = destination / relative_path.parent
            ensure_directory(target_dir)
            target = target_dir / f"{relative_path.name}.last200.txt"
            try:
                content = tail_file(source)
            except RuntimeError as exc:
                errors.append(str(exc))
                continue
            with target.open("w", encoding="utf-8") as handle:
                handle.write(content)
            created.append(target)
    return created


def write_file_list(
    target_dir: Path,
    root: Path,
    destination: Path,
    errors: list[str],
) -> tuple[int, Path | None]:
    if not target_dir.exists():
        return 0, None
    try:
        entries = list_files_within(target_dir, root)
    except OSError as exc:
        errors.append(f"Unable to enumerate {target_dir}: {exc}")
        return 0, None
    ensure_directory(destination.parent)
    with destination.open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(f"{entry}\n")
    return len(entries), destination


def write_ctl_db_metadata(path: Path, destination: Path, errors: list[str]) -> Path | None:
    if not path.exists():
        errors.append(f"{path} not found")
        return None
    try:
        stat = path.stat()
        checksum = sha256_file(path)
    except OSError as exc:
        errors.append(f"Unable to read metadata for {path}: {exc}")
        return None
    ensure_directory(destination.parent)
    mtime = _dt.datetime.fromtimestamp(stat.st_mtime).isoformat()
    with destination.open("w", encoding="utf-8") as handle:
        handle.write(f"path: {path}\n")
        handle.write(f"size_bytes: {stat.st_size}\n")
        handle.write(f"modified_at: {mtime}\n")
        handle.write(f"sha256: {checksum}\n")
    return destination


def create_summary(
    destination: Path,
    *,
    timestamp: str,
    env_flags: list[str],
    channel_info: list[str],
    role_map_info: list[str],
    counts: dict[str, int],
    recent_files: list[tuple[str, float]],
    errors: list[str],
) -> None:
    ensure_directory(destination.parent)
    lines: list[str] = []
    lines.append("# Centrix Diagnostics Summary")
    lines.append("")
    lines.append(f"- Timestamp: {timestamp}")
    lines.append(f"- OS: {platform.platform()}")
    lines.append(f"- Python Version: {platform.python_version()}")
    lines.append("")
    lines.append("## ENV Flags")
    if env_flags:
        for entry in env_flags:
            lines.append(f"- {entry}")
    else:
        lines.append("- none")
    lines.append("")
    lines.append("## Channels / Role Map")
    if channel_info:
        lines.append("- Channels:")
        for entry in channel_info:
            lines.append(f"  - {entry}")
    else:
        lines.append("- Channels: none")
    if role_map_info:
        lines.append("- Role Map:")
        for entry in role_map_info:
            lines.append(f"  - {entry}")
    else:
        lines.append("- Role Map: none")
    lines.append("")
    lines.append("## Directory File Counts")
    for key in ("runtime", "logs", "reports"):
        lines.append(f"- {key}: {counts.get(key, 0)} files")
    lines.append("")
    lines.append("## Recently Modified Files")
    if recent_files:
        for rel_path, mtime in recent_files:
            iso_mtime = _dt.datetime.fromtimestamp(mtime).isoformat()
            lines.append(f"- {rel_path} ({iso_mtime})")
    else:
        lines.append("- none")
    if errors:
        lines.append("")
        lines.append("## Errors")
        for entry in errors:
            lines.append(f"- {entry}")

    ensure_directory(destination.parent)
    with destination.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))


def main(args: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Centrix diagnostics bundle.")
    parser.parse_args(args)

    script_path = Path(__file__).resolve()
    repo_root = script_path.parents[1]
    timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    diagnostics_root = repo_root / "diagnostics"
    output_dir = diagnostics_root / f"centrix_diag_{timestamp}"
    recent_logs_dir = output_dir / "recent_logs"
    errors: list[str] = []
    created_files: list[Path] = []

    recent_files = gather_recent_files(repo_root, exclude_dirs=("diagnostics",))

    ensure_directory(output_dir)

    # Directory tree
    tree_output = build_tree(repo_root, MAX_TREE_DEPTH)
    tree_path = output_dir / "tree.txt"
    with tree_path.open("w", encoding="utf-8") as handle:
        handle.write(tree_output)
    created_files.append(tree_path)

    # Directory listings
    counts: dict[str, int] = {}
    listing_specs = {
        "runtime": repo_root / "runtime",
        "logs": repo_root / "logs",
        "reports": repo_root / "reports",
    }
    for name, target_dir in listing_specs.items():
        list_path = output_dir / f"{name}_files.txt"
        count, path_written = write_file_list(target_dir, repo_root, list_path, errors)
        counts[name] = count
        if path_written:
            created_files.append(list_path)
        else:
            try:
                list_path.unlink(missing_ok=True)
            except AttributeError:
                if list_path.exists():
                    list_path.unlink()

    # Recent log tails
    log_files = collect_logs(repo_root, recent_logs_dir, errors)
    created_files.extend(log_files)

    # Sanitize .env
    env_path = repo_root / ".env"
    sanitized_env_path = output_dir / "env_sanitized.env"
    sanitized_pairs: list[tuple[str, str]] = []
    env_flags: list[str] = []
    if env_path.exists():
        env_entries = read_env_file(env_path)
        sanitized_pairs, env_flags = sanitize_env(env_entries, sanitized_env_path)
        if sanitized_env_path.exists():
            created_files.append(sanitized_env_path)
    else:
        errors.append(".env not found")

    # Channel and role map info
    channel_info: list[str] = []
    role_map_info: list[str] = []
    for key, value in sanitized_pairs:
        if not key:
            continue
        upper_key = key.upper()
        if "CHANNEL" in upper_key and value:
            channel_info.append(f"{key}={mask_identifier(value)}")
        if key == "SLACK_ROLE_MAP" and value:
            role_map = parse_role_map(value)
            for user_id, role in role_map.items():
                role_map_info.append(f"{mask_identifier(user_id)} → {role}")

    # ctl.db metadata
    ctl_db_path = repo_root / "runtime" / "ctl.db"
    ctl_meta_path = output_dir / "runtime_ctl_db.txt"
    ctl_metadata_file = write_ctl_db_metadata(ctl_db_path, ctl_meta_path, errors)
    if ctl_metadata_file:
        created_files.append(ctl_meta_path)

    # Summary
    summary_path = output_dir / "summary.md"
    create_summary(
        summary_path,
        timestamp=timestamp,
        env_flags=env_flags,
        channel_info=channel_info,
        role_map_info=role_map_info,
        counts=counts,
        recent_files=recent_files,
        errors=errors,
    )
    created_files.append(summary_path)

    # Zip bundle
    zip_name = f"centrix_diag_{timestamp}.zip"
    zip_path = diagnostics_root / zip_name
    ensure_directory(diagnostics_root)
    shutil.make_archive(zip_path.with_suffix(""), "zip", root_dir=output_dir.parent, base_dir=output_dir.name)
    created_files.append(zip_path)

    for path in created_files:
        print(path.resolve())

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
