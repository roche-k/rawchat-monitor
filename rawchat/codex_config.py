"""Managed Codex configuration takeover."""

import os
import re
from pathlib import Path

from .config import tomllib


class CodexConfigManager:
    """原子接管 Codex 配置，并保留所有非受管内容。"""

    _MANAGED_SECTION = "model_providers.rawchat_monitor"

    def __init__(self, config_path: str | os.PathLike[str], port: int) -> None:
        self.config_path = Path(config_path).expanduser()
        self.port = port
        self._applied = False

    @staticmethod
    def _heading(line: str) -> str | None:
        match = re.match(r"^\s*\[\[?([^\]]+)\]\]?\s*(?:#.*)?$", line)
        return match.group(1).strip() if match else None

    def _provider_text(self, newline: str) -> str:
        return newline.join(
            [
                "[model_providers.rawchat_monitor]",
                'name = "RawChat Monitor"',
                f'base_url = "http://127.0.0.1:{self.port}/v1"',
                'wire_api = "responses"',
                "requires_openai_auth = false",
                "",
            ]
        )

    def _render(self, original: str) -> str:
        newline = "\r\n" if "\r\n" in original else "\n"
        lines = original.splitlines(keepends=True)
        provider_assignment = 'model_provider = "rawchat_monitor"'
        found_assignment = False
        section: str | None = None
        replaced_lines: list[str] = []
        for line in lines:
            heading = self._heading(line.rstrip("\r\n"))
            if heading is not None:
                section = heading
            if section is None and re.match(r"^\s*model_provider\s*=", line):
                ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                replaced_lines.append(provider_assignment + ending)
                found_assignment = True
            else:
                replaced_lines.append(line)
        if not found_assignment:
            replaced_lines.insert(0, provider_assignment + newline)

        headings: list[tuple[int, str]] = []
        for index, line in enumerate(replaced_lines):
            heading = self._heading(line.rstrip("\r\n"))
            if heading is not None:
                headings.append((index, heading))

        managed_start: int | None = None
        managed_end: int | None = None
        for heading_index, heading in headings:
            if heading != self._MANAGED_SECTION:
                continue
            managed_start = heading_index
            managed_end = len(replaced_lines)
            for next_index, next_heading in headings:
                if next_index <= heading_index:
                    continue
                if not next_heading.startswith(self._MANAGED_SECTION + "."):
                    managed_end = next_index
                    break
            break

        provider_lines = self._provider_text(newline).splitlines(keepends=True)
        if managed_start is not None and managed_end is not None:
            replaced_lines = (
                replaced_lines[:managed_start]
                + provider_lines
                + replaced_lines[managed_end:]
            )
        else:
            if replaced_lines and not replaced_lines[-1].endswith(("\n", "\r")):
                replaced_lines.append(newline)
            if replaced_lines and replaced_lines[-1].strip():
                replaced_lines.append(newline)
            replaced_lines.extend(provider_lines)
        return "".join(replaced_lines)

    @staticmethod
    def _write_atomic(path: Path, data: bytes, mode: int = 0o600) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.tmp")
        temp_path.write_bytes(data)
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)

    def apply(self) -> None:
        if tomllib is None:
            raise RuntimeError("TOML parser unavailable")
        if self._applied:
            raise RuntimeError("Codex 配置已经检查")
        try:
            original = self.config_path.read_bytes()
            original_mode = os.stat(self.config_path).st_mode & 0o777
        except FileNotFoundError:
            original = b""
            original_mode = 0o600
        original_text = original.decode("utf-8")
        rendered = self._render(original_text)
        tomllib.loads(rendered)
        rendered_bytes = rendered.encode("utf-8")
        if rendered_bytes != original:
            self._write_atomic(self.config_path, rendered_bytes, original_mode)
        self._applied = True
