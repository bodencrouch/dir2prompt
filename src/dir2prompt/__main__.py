#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fnmatch
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, TextIO, Tuple

VERSION = "python-port"


@dataclass
class Target:
    name: str
    directory: Path
    ignore_files: List[Path] = field(default_factory=list)
    max_depth: Optional[int] = None
    max_filesize: Optional[int] = None
    file_type: Optional[str] = None
    view: Optional[str] = None
    add_rules: List[str] = field(default_factory=list)
    drop_rules: List[str] = field(default_factory=list)
    add_rule_files: List[Path] = field(default_factory=list)
    follow_symlinks: bool = False


@dataclass
class Pattern:
    text: str
    include: bool

    def matches(self, relative: str, is_dir: bool = False) -> bool:
        value = self.text.rstrip()
        if not value or value.startswith("#"):
            return False
        if value.startswith("!"):
            value = value[1:]
        anchored = value.startswith("/")
        value = value.lstrip("/")
        directory_only = value.endswith("/")
        value = value.rstrip("/")
        if directory_only and not is_dir and not relative.startswith(value + "/"):
            return False
        candidates = [relative] if anchored or "/" in value else [relative, Path(relative).name]
        return any(fnmatch.fnmatchcase(candidate, value) or fnmatch.fnmatchcase(candidate, value + "/**") for candidate in candidates)


def read_patterns(paths: Iterable[Path]) -> List[Pattern]:
    result: List[Pattern] = []
    for path in paths:
        if not path.is_file():
            raise SystemExit(f"Ignore/rule file does not exist: {path}")
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                result.append(Pattern(line, line.startswith("!")))
    return result


def config_dir(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".dir2prompt").is_dir():
            return candidate / ".dir2prompt"
    return current / ".dir2prompt"


def load_view(target: Target) -> List[Path]:
    if not target.view and not target.add_rules:
        return []
    try:
        import yaml  # type: ignore
    except ImportError as exc:
        raise SystemExit("Views require PyYAML: python -m pip install PyYAML") from exc
    root = config_dir(target.directory)
    views_file = root / "views.yml"
    data = yaml.safe_load(views_file.read_text(encoding="utf-8")) if views_file.exists() else {}
    data = data or {}
    views = data.get("views", data)
    view = views.get(target.view, {}) if target.view else {}
    names = list(view.get("rules", [])) if isinstance(view, dict) else list(view or [])
    names.extend(target.add_rules)
    names = [name for name in names if name not in target.drop_rules]
    return [root / "rules" / f"{name}.ignore" for name in names]


def selected_files(target: Target) -> Tuple[List[Path], List[Pattern]]:
    root = target.directory.resolve()
    automatic: List[Path] = []
    if not target.ignore_files:
        for candidate in (root / ".promptignore", config_dir(root).parent / ".promptignore"):
            if candidate.is_file() and candidate not in automatic:
                automatic.append(candidate)
    patterns = read_patterns([*automatic, *target.ignore_files, *load_view(target), *target.add_rule_files])
    has_includes = any(item.include for item in patterns)
    output: List[Path] = []
    for base, dirs, files in os.walk(str(root), followlinks=target.follow_symlinks):
        base_path = Path(base)
        depth = len(base_path.relative_to(root).parts)
        if target.max_depth is not None and depth >= target.max_depth:
            dirs[:] = []
        dirs[:] = sorted(d for d in dirs if d != ".git")
        for name in sorted(files):
            path = base_path / name
            relative = path.relative_to(root).as_posix()
            if target.max_depth is not None and len(path.relative_to(root).parts) > target.max_depth:
                continue
            if target.max_filesize is not None and path.stat().st_size > target.max_filesize:
                continue
            if target.file_type and path.suffix.lstrip(".").lower() != target.file_type.lstrip(".").lower():
                continue
            included = not has_includes
            for pattern in patterns:
                if pattern.matches(relative):
                    included = pattern.include
            if included:
                output.append(path)
    return output, patterns


def binary(path: Path) -> bool:
    try:
        return b"\0" in path.read_bytes()[:8192]
    except OSError:
        return True


def render_tree(root: Path, files: Sequence[Path], stream: TextIO) -> None:
    tree: dict = {}
    for path in files:
        node = tree
        for part in path.relative_to(root).parts:
            node = node.setdefault(part, {})

    def visit(node: dict, prefix: str = "") -> None:
        entries = sorted(node.items(), key=lambda item: (not item[1], item[0].lower()))
        for index, (name, children) in enumerate(entries):
            last = index == len(entries) - 1
            print(prefix + ("└── " if last else "├── ") + name, file=stream)
            if children:
                visit(children, prefix + ("    " if last else "│   "))

    print(root.name or str(root), file=stream)
    visit(tree)


def mapped_content(path: Path, mappings: Sequence[Tuple[str, str]]) -> Optional[str]:
    command: Optional[str] = None
    for pattern, candidate in mappings:
        if fnmatch.fnmatchcase(path.name, pattern) or fnmatch.fnmatchcase(path.as_posix(), pattern):
            command = candidate
    if command == "raw" or command is None:
        if binary(path):
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    completed = subprocess.run(command, shell=True, input=path.read_bytes(), stdout=subprocess.PIPE, check=True)
    return completed.stdout.decode("utf-8", errors="replace")


def emit(target: Target, mode: str, mappings: Sequence[Tuple[str, str]], manifest: str, stream: TextIO) -> None:
    files, patterns = selected_files(target)
    root = target.directory.resolve()
    if mode != "contents":
        render_tree(root, files, stream)
    if mode != "tree":
        for path in files:
            content = mapped_content(path, mappings)
            if content is None:
                continue
            print(f"\n--- {path.relative_to(root).as_posix()} ---", file=stream)
            print(content.rstrip("\n"), file=stream)
    if manifest != "off":
        print("\n--- selection manifest ---", file=stream)
        print(f"target: {target.name}", file=stream)
        print(f"directory: {root}", file=stream)
        print(f"selected-files: {len(files)}", file=stream)
        if manifest in {"full", "llm"}:
            print("patterns:", file=stream)
            for item in patterns:
                print(f"  - {item.text}", file=stream)


def rules_command(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(prog="dir2prompt rules")
    sub = parser.add_subparsers(dest="action", required=True)
    add = sub.add_parser("add")
    add.add_argument("name")
    add.add_argument("--from-file", type=Path)
    add.add_argument("--description")
    add.add_argument("--view")
    add.add_argument("--base-view")
    sub.add_parser("list")
    show = sub.add_parser("show")
    show.add_argument("name")
    sub.add_parser("init")
    args = parser.parse_args(argv)
    root = config_dir(Path.cwd())
    rules = root / "rules"
    if args.action == "init":
        rules.mkdir(parents=True, exist_ok=True)
        (root / "views.yml").write_text("default_view: default\nviews:\n  default:\n    description: Default project view\n    rules: [baseline]\n", encoding="utf-8")
        (rules / "baseline.ignore").write_text("# Add exclusions, or !patterns to focus the selection\n", encoding="utf-8")
        print(f"Initialized {root}")
    elif args.action == "add":
        if not all(c.isalnum() or c in "-_" for c in args.name):
            parser.error("rule names may contain only letters, digits, hyphens, and underscores")
        rules.mkdir(parents=True, exist_ok=True)
        text = args.from_file.read_text(encoding="utf-8") if args.from_file else sys.stdin.read()
        (rules / f"{args.name}.ignore").write_text(text, encoding="utf-8")
        print(f"Wrote rule {args.name}")
    elif args.action == "show":
        path = rules / f"{args.name}.ignore"
        print(path.read_text(encoding="utf-8"), end="")
    else:
        print("Rules:")
        for path in sorted(rules.glob("*.ignore")):
            print(f"  {path.stem}: {path}")
        views = root / "views.yml"
        if views.exists():
            print(f"Views: {views}")
    return 0


def parse_map(value: str) -> Tuple[str, str]:
    if ":" not in value:
        raise argparse.ArgumentTypeError("mapping must be GLOB:CMD")
    return tuple(value.split(":", 1))  # type: ignore[return-value]


def main(argv: Optional[Sequence[str]] = None) -> int:
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] == ["rules"]:
        return rules_command(args_list[1:])
    parser = argparse.ArgumentParser(description="Create a directory snapshot for LLM prompts")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--contents-only", action="store_true")
    mode.add_argument("--tree-only", action="store_true")
    parser.add_argument("--manifest", nargs="?", const="summary", choices=("summary", "full", "llm"), default="off")
    parser.add_argument("--map", action="append", type=parse_map, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--ignore-file", action="append", type=Path, default=[])
    parser.add_argument("--max-depth", type=int)
    parser.add_argument("--max-filesize", type=int)
    parser.add_argument("--type", dest="file_type")
    parser.add_argument("--view")
    parser.add_argument("--add-rule", action="append", default=[])
    parser.add_argument("--drop-rule", action="append", default=[])
    parser.add_argument("--add-rule-file", action="append", type=Path, default=[])
    parser.add_argument("--follow-symlinks", action="store_true")
    parser.add_argument("--no-follow-symlinks", action="store_true")
    parser.add_argument("directories", nargs="*", type=Path)
    args = parser.parse_args(args_list)
    directories = args.directories or [Path.cwd()]
    targets = [Target(path.name or str(path), path, args.ignore_file, args.max_depth, args.max_filesize, args.file_type, args.view, args.add_rule, args.drop_rule, args.add_rule_file, args.follow_symlinks and not args.no_follow_symlinks) for path in directories]
    output: TextIO = args.output.open("w", encoding="utf-8") if args.output else sys.stdout
    try:
        selected_mode = "contents" if args.contents_only else "tree" if args.tree_only else "both"
        for index, target in enumerate(targets):
            if index:
                print(file=output)
            emit(target, selected_mode, args.map, args.manifest, output)
    finally:
        if args.output:
            output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
