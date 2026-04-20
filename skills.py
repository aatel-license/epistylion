"""
epistylion.skills
~~~~~~~~~~~~~~~~~

Carica, indicizza e inietta skill (file SKILL.md) nel system prompt
dell'agente Epistylion prima di ogni chiamata al modello.

Struttura cartella skill::

    skills/
        code/SKILL.md
        translate/SKILL.md
        summarize/SKILL.md
        ...

Ogni SKILL.md contiene istruzioni in linguaggio naturale che vengono
iniettate nel system prompt del ReAct loop prima che il modello risponda.
La skill non è un tool — non viene chiamata durante il loop, non appare
nella tool list. Plasma il comportamento del modello dall'inizio.

Uso tipico via MCPBridge::

    async with MCPBridge.from_config("mcp_servers.json") as bridge:
        await bridge.connect(skill="code")
        result = await bridge.agent.run("Scrivi un parser JSON")

Uso avanzato — skill diversa per ogni run()::

    await bridge.connect()                          # nessuna skill default
    r1 = await bridge.agent.run("...", skill="code")
    r2 = await bridge.agent.run("...", skill="translate")
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ── Costanti ───────────────────────────────────────────────────────────────────
_DEFAULT_SKILLS_DIR = Path(os.getenv("EPISTYLION_SKILLS_DIR", "skills"))

SEPARATOR = "\n\n---\n\n"   # separatore tra skill e system prompt base


# ── Dataclass ──────────────────────────────────────────────────────────────────
@dataclass
class Skill:
    name:        str    # es. "code"
    path:        Path   # percorso al SKILL.md
    description: str    # prima riga non vuota (usata come label nei log)
    content:     str    # contenuto completo del file


# ── Registry ───────────────────────────────────────────────────────────────────
class SkillRegistry:
    """
    Indicizza tutti i SKILL.md trovati nella cartella skills/ e nelle
    sue sottocartelle. Permette di iniettare una skill nel system prompt.

    Parameters
    ----------
    skills_dir : str | Path
        Cartella root delle skill. Default: variabile d'ambiente
        EPISTYLION_SKILLS_DIR, oppure ./skills.
    """

    def __init__(self, skills_dir: str | Path = _DEFAULT_SKILLS_DIR) -> None:
        self.skills_dir = Path(skills_dir)
        self._registry: dict[str, Skill] = {}
        if self.skills_dir.exists():
            self._scan()

    # ── scan ──────────────────────────────────────────────────────────────────
    def _scan(self) -> None:
        for skill_file in sorted(self.skills_dir.rglob("SKILL.md")):
            # nome = cartella immediata (es. skills/code/SKILL.md → "code")
            name = skill_file.parent.name.lower()
            self._registry[name] = self._load(name, skill_file)

    def _load(self, name: str, path: Path) -> Skill:
        content     = path.read_text(encoding="utf-8")
        description = next(
            (ln.lstrip("# ").strip() for ln in content.splitlines() if ln.strip()),
            name,
        )
        return Skill(name=name, path=path, description=description, content=content)

    # ── API pubblica ──────────────────────────────────────────────────────────
    def list(self) -> list[str]:
        """Restituisce i nomi di tutte le skill disponibili."""
        return sorted(self._registry)

    def get(self, name: str) -> Skill | None:
        """Restituisce una skill per nome (case-insensitive), o None."""
        return self._registry.get(name.lower())

    def load_path(self, path: str | Path) -> Skill:
        """Carica una skill direttamente da un percorso file."""
        p    = Path(path)
        name = p.parent.name.lower() if p.name == "SKILL.md" else p.stem.lower()
        return self._load(name, p)

    def apply(
        self,
        skill_name_or_path: str,
        base_system: str = "Sei un assistente utile con accesso a vari tool MCP.",
    ) -> str:
        """
        Restituisce il system prompt finale con la skill iniettata in testa.

        Il contenuto della skill precede sempre il system prompt base,
        così le istruzioni della skill hanno la massima priorità.

        Parameters
        ----------
        skill_name_or_path : str
            Nome registrato (es. "code") oppure percorso diretto a un SKILL.md.
        base_system : str
            System prompt base dell'agente.

        Returns
        -------
        str
            System prompt finale: ``<skill content> --- <base_system>``

        Raises
        ------
        FileNotFoundError
            Se la skill non viene trovata né per nome né per percorso.
        """
        skill = self._resolve(skill_name_or_path)
        if skill is None:
            available = ", ".join(self.list()) or "nessuna"
            raise FileNotFoundError(
                f"Skill '{skill_name_or_path}' non trovata. "
                f"Disponibili: {available}"
            )
        return skill.content + SEPARATOR + base_system

    def _resolve(self, name_or_path: str) -> Skill | None:
        p = Path(name_or_path)
        if p.exists():
            return self.load_path(p)
        return self.get(name_or_path)

    # ── repr ──────────────────────────────────────────────────────────────────
    def __repr__(self) -> str:
        return f"SkillRegistry(dir={self.skills_dir!r}, skills={self.list()})"


# ── Istanza globale (lazy) ─────────────────────────────────────────────────────
_registry: SkillRegistry | None = None


def get_registry(skills_dir: str | Path | None = None) -> SkillRegistry:
    """Restituisce (o crea) il registry globale condiviso."""
    global _registry
    if _registry is None or skills_dir is not None:
        _registry = SkillRegistry(skills_dir or _DEFAULT_SKILLS_DIR)
    return _registry
