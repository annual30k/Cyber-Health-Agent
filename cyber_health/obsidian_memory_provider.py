"""Filesystem-backed provider for the configured health-manager memory scope.

The provider is deliberately outside the domain core.  It shares the
Obsidian Memory lifecycle (Inbox -> Raw -> Wiki) with the configured
health-manager project, while the core only sees the MemoryProvider protocol.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Iterable

from .memory import MemoryUnavailable


_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


def _has_symlink_in_path(path: Path) -> bool:
    current = path
    while True:
        if current.is_symlink():
            return True
        if current.parent == current:
            return False
        current = current.parent


def _project_is_declared(vault: Path, project_id: str) -> bool:
    registry = vault / "00-System" / "projects.yaml"
    if not registry.is_file():
        return False
    try:
        text = registry.read_text(encoding="utf-8")
    except OSError:
        return False
    return bool(re.search(rf"(?m)^\s*- id:\s*{re.escape(project_id)}\s*$", text))


class ObsidianMemoryProvider:
    """Bridge Cyber Health memory intents to one private Obsidian project."""

    def __init__(self, vault_path: str | Path, project_id: str):
        self.vault_path = Path(vault_path).expanduser()
        self.project_id = project_id
        self.project_path = self.vault_path / "20-Projects" / project_id
        self._validate_scope()

    def _validate_scope(self) -> None:
        if not self.vault_path.is_absolute() or not self.project_id or not _SAFE_COMPONENT.fullmatch(self.project_id):
            raise MemoryUnavailable("Invalid health-manager memory scope")
        if _has_symlink_in_path(self.vault_path) or _has_symlink_in_path(self.project_path):
            raise MemoryUnavailable("Health-manager memory scope contains a symlink")
        if not self.vault_path.is_dir() or not os.access(self.vault_path, os.R_OK | os.X_OK):
            raise MemoryUnavailable(f"Obsidian Vault is unavailable: {self.vault_path}")
        if not self.project_path.is_dir() or not os.access(self.project_path, os.R_OK | os.X_OK):
            raise MemoryUnavailable(f"Health-manager project is unavailable: {self.project_path}")
        if self.vault_path.resolve() not in self.project_path.resolve().parents:
            raise MemoryUnavailable("Health-manager project escapes the configured Vault")
        if not _project_is_declared(self.vault_path, self.project_id):
            raise MemoryUnavailable("Health-manager project is not declared in Vault/00-System/projects.yaml")

    def _safe_dir(self, name: str) -> Path:
        path = self.project_path / name
        if not path.is_dir() or not self._inside(path):
            raise MemoryUnavailable(f"Memory scope directory is unavailable: {path}")
        return path

    def _inside(self, path: Path) -> bool:
        try:
            return self.project_path.resolve() in path.resolve().parents or path.resolve() == self.project_path.resolve()
        except OSError as exc:
            raise MemoryUnavailable(f"Cannot resolve memory path: {path}") from exc

    def _safe_note(self, path: Path) -> Path:
        if path.suffix != ".md" or not self._inside(path) or path.is_symlink():
            raise MemoryUnavailable(f"Unsafe memory note path: {path}")
        return path

    @staticmethod
    def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
        if not text.startswith("---\n"):
            return {}, text
        end = text.find("\n---", 4)
        if end < 0:
            return {}, text
        fields: dict[str, str] = {}
        for line in text[4:end].splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip().strip('"')
        return fields, text[end + 4 :].lstrip("\n")

    @staticmethod
    def _yaml_value(value: Any) -> str:
        if value is None:
            return "null"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return str(value).replace("\n", " ").replace("\r", " ").replace('"', "'")

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass

    def _iter_notes(self, directory: Path) -> Iterable[Path]:
        if not directory.exists():
            return []
        notes: list[Path] = []
        paths = directory.glob("*.md") if directory == self.project_path else directory.rglob("*.md")
        for path in paths:
            try:
                relative = path.relative_to(self.project_path)
                if relative.parts and relative.parts[0] == "inbox":
                    continue
                # AGENTS.md is host instruction data, never a health-memory result.
                if path.name == "AGENTS.md":
                    continue
                if path.is_file() and not path.is_symlink() and self._inside(path):
                    notes.append(path)
            except OSError:
                continue
        return notes

    def _query(self, payload: dict[str, Any]) -> dict[str, Any]:
        query = str(payload.get("query") or "").strip().lower()
        limit = max(1, min(int(payload.get("limit") or 10), 50))
        terms = [term for term in re.split(r"\s+", query) if len(term) > 1]
        if query and query not in terms:
            terms.append(query)
        items: list[dict[str, Any]] = []
        # rules.md/index.md/log.md are workflow/control files.  The memory
        # skill recalls project knowledge from Wiki, Raw and Checkpoints only.
        for directory in (self._safe_dir("wiki"), self._safe_dir("raw"), self._safe_dir("checkpoints")):
            for note in self._iter_notes(directory):
                try:
                    text = note.read_text(encoding="utf-8")
                except OSError:
                    continue
                fields, body = self._parse_frontmatter(text)
                haystack = f"{note.name} {text}".lower()
                if terms and not any(term in haystack for term in terms):
                    continue
                item_id = str(note.relative_to(self.project_path))
                items.append(
                    {
                        "id": item_id,
                        "candidate_id": fields.get("candidate_id") or fields.get("id") or item_id,
                        "content": body[:4000],
                        "occurred_at": fields.get("occurred_at") or fields.get("last_updated"),
                        "confidence": self._float_or_none(fields.get("confidence")),
                        "confirmation_status": fields.get("status") or "unconfirmed",
                        "source_path": item_id,
                    }
                )
                if len(items) >= limit:
                    return {"items": items[:limit], "provider": "obsidian", "project_id": self.project_id}
        return {"items": items[:limit], "provider": "obsidian", "project_id": self.project_id}

    @staticmethod
    def _float_or_none(value: str | None) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _propose(self, payload: dict[str, Any]) -> dict[str, Any]:
        intent_id = str(payload.get("intent_id") or "")
        user_id = str(payload.get("user_id") or "")
        if not intent_id or not _SAFE_COMPONENT.fullmatch(intent_id):
            raise MemoryUnavailable("Memory proposal has no safe intent_id")
        inbox = self._safe_dir("inbox")
        candidate = self._safe_note(inbox / f"cand-{intent_id}.md")
        if candidate.exists():
            return {
                "status": "pending",
                "provider": "obsidian",
                "candidate_id": candidate.stem,
                "path": str(candidate.relative_to(self.project_path)),
                "idempotent_replay": True,
            }

        now = datetime.now(timezone.utc).isoformat()
        method = str(payload.get("method") or "memory.propose")
        evidence = payload.get("evidence") or payload.get("content") or payload.get("summary") or payload
        title = str(payload.get("title") or "Cyber Health 长期记忆候选")
        frontmatter = {
            "schema_version": 1,
            "id": candidate.stem,
            "candidate_id": candidate.stem,
            "title": title,
            "type": "health-memory-candidate",
            "scope": "project",
            "project_id": self.project_id,
            "user_id": user_id,
            "status": "pending-ingest",
            "created": now,
            "sensitivity": "internal",
            "source_ref": "Cyber Health MemoryProvider",
            "source_method": method,
        }
        lines = ["---"] + [f'{key}: "{self._yaml_value(value)}"' for key, value in frontmatter.items()] + [
            "---",
            "",
            f"# {title}",
            "",
            "## Evidence",
            "",
            "```json",
            json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
            "## Lifecycle",
            "",
            "This candidate requires the health-manager memory workflow and user confirmation before Raw/Wiki promotion.",
            "",
        ]
        self._atomic_write(candidate, "\n".join(lines))
        return {
            "status": "pending",
            "provider": "obsidian",
            "candidate_id": candidate.stem,
            "path": str(candidate.relative_to(self.project_path)),
            "idempotent_replay": False,
        }

    def _action(self, payload: dict[str, Any]) -> dict[str, Any]:
        candidate_id = str(payload.get("candidate_id") or "")
        action = str(payload.get("action_type") or payload.get("action") or "action")
        if not candidate_id or not _SAFE_COMPONENT.fullmatch(candidate_id):
            raise MemoryUnavailable("Memory action has no safe candidate_id")
        candidate = self._safe_note(self._safe_dir("inbox") / f"{candidate_id}.md")
        if not candidate.exists():
            raise MemoryUnavailable(f"Memory candidate does not exist: {candidate_id}")
        text = candidate.read_text(encoding="utf-8")
        fields, body = self._parse_frontmatter(text)
        if action in {"reject", "delete"} or payload.get("confirmed") is False and action == "reject":
            updated = self._replace_status(text, "rejected")
            self._atomic_write(candidate, updated)
            return {"status": "rejected", "candidate_id": candidate_id, "path": str(candidate.relative_to(self.project_path))}
        if not payload.get("confirmed") and action not in {"confirm", "approve", "ingest"}:
            return {"status": "pending", "candidate_id": candidate_id, "path": str(candidate.relative_to(self.project_path))}

        # Check the complete ingest receipt boundary before creating Raw/Wiki,
        # so a missing self-growing index/log cannot leave a half-promoted note.
        for managed_name in ("index.md", "log.md"):
            managed = self._safe_note(self.project_path / managed_name)
            if not managed.is_file():
                raise MemoryUnavailable(f"Health-manager project {managed_name} is missing")

        raw_dir = self._safe_dir("raw")
        wiki_dir = self._safe_dir("wiki") / "knowledge"
        raw = self._safe_note(raw_dir / f"raw-{candidate_id}.md")
        wiki = self._safe_note(wiki_dir / f"{candidate_id}.md")
        title = fields.get("title") or "Cyber Health 长期规律"

        # A confirmed candidate must not create a duplicate canonical page.
        for existing in self._iter_notes(self._safe_dir("wiki")):
            if existing == wiki:
                continue
            try:
                existing_fields, _ = self._parse_frontmatter(existing.read_text(encoding="utf-8"))
            except OSError:
                continue
            if existing_fields.get("title") == title and existing_fields.get("status") == "confirmed_wiki":
                self._atomic_write(candidate, self._replace_status(text, "ingested"))
                return {
                    "status": "duplicate",
                    "candidate_id": candidate_id,
                    "wiki_path": str(existing.relative_to(self.project_path)),
                }

        if not raw.exists():
            raw_frontmatter = dict(fields)
            raw_frontmatter.update({"id": f"raw-{candidate_id}", "candidate_id": candidate_id, "status": "raw"})
            raw_text = "---\n" + "\n".join(
                f'{key}: "{self._yaml_value(value)}"' for key, value in raw_frontmatter.items()
            ) + "\n---\n\n" + body
            self._atomic_write(raw, raw_text)
        if not wiki.exists():
            wiki_frontmatter = {
                "title": title,
                "type": "health-knowledge",
                "status": "confirmed_wiki",
                "created": fields.get("created") or datetime.now(timezone.utc).isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "sensitivity": fields.get("sensitivity") or "internal",
                "project_id": self.project_id,
                "sources": f"[[20-Projects/{self.project_id}/raw/raw-{candidate_id}]]",
            }
            wiki_text = "---\n" + "\n".join(
                f'{key}: "{self._yaml_value(value)}"' for key, value in wiki_frontmatter.items()
            ) + "\n---\n\n" + body
            self._atomic_write(wiki, wiki_text)
        self._update_index_and_log(title, candidate_id, raw, wiki)
        self._atomic_write(candidate, self._replace_status(text, "ingested"))
        return {
            "status": "confirmed_wiki",
            "candidate_id": candidate_id,
            "raw_path": str(raw.relative_to(self.project_path)),
            "wiki_path": str(wiki.relative_to(self.project_path)),
        }

    def _update_index_and_log(self, title: str, candidate_id: str, raw: Path, wiki: Path) -> None:
        """Complete the self-growing ingest receipt without replacing human text."""
        index = self._safe_note(self.project_path / "index.md")
        log = self._safe_note(self.project_path / "log.md")
        if not index.is_file() or not log.is_file():
            raise MemoryUnavailable("Health-manager project index.md or log.md is missing")

        index_text = index.read_text(encoding="utf-8")
        index_entry = f"- [[wiki/knowledge/{wiki.stem}|{title}]]"
        if index_entry not in index_text:
            placeholder = "_No knowledge pages yet._"
            if placeholder in index_text:
                index_text = index_text.replace(placeholder, index_entry, 1)
            elif "## Knowledge" in index_text:
                index_text = index_text.replace("## Knowledge\n", f"## Knowledge\n\n{index_entry}\n", 1)
            else:
                index_text = index_text.rstrip() + f"\n\n## Knowledge\n\n{index_entry}\n"
            self._atomic_write(index, index_text)

        day = datetime.now().astimezone().date().isoformat()
        log_marker = f"## [{day}] ingest | {title}"
        log_text = log.read_text(encoding="utf-8")
        if log_marker not in log_text:
            receipt = (
                f"\n\n{log_marker}\n\n"
                f"- Candidate: [[inbox/{candidate_id}]]\n"
                f"- Raw: [[{raw.relative_to(self.project_path)}]]\n"
                f"- Wiki: [[{wiki.relative_to(self.project_path)}]]\n"
            )
            self._atomic_write(log, log_text.rstrip() + receipt)

    @staticmethod
    def _replace_status(text: str, status: str) -> str:
        replacement = f'status: "{status}"'
        if re.search(r"(?m)^status:\s*.*$", text):
            return re.sub(r"(?m)^status:\s*.*$", replacement, text, count=1)
        return text

    def call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = method.removeprefix("memory.")
        if normalized == "ping":
            return {
                "status": "ok",
                "provider": "obsidian",
                "vault_path": str(self.vault_path),
                "project_id": self.project_id,
            }
        if normalized == "query":
            return self._query(payload)
        if normalized in {"propose", "propose_candidate"}:
            return self._propose(payload)
        if normalized in {"action", "confirm", "approve", "reject", "delete", "update"}:
            action_payload = dict(payload)
            action_payload.setdefault("action_type", normalized)
            return self._action(action_payload)
        if normalized == "maintain":
            pending = len(list(self._iter_notes(self._safe_dir("inbox"))))
            return {"status": "ok", "provider": "obsidian", "pending_candidates": pending}
        raise MemoryUnavailable(f"Unsupported Obsidian MemoryProvider method: {method}")
