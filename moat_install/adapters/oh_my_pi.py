"""oh-my-pi (``omp``) adapter — mirrors pi's ``models.json`` format.

RESEARCH findings (verified offline against the installed ``@oh-my-pi``
distribution): oh-my-pi is a pi distribution; its core package is
``@oh-my-pi/pi-coding-agent`` ("Core implementation package for the omp coding
agent"). Its agent directory defaults to ``~/.omp/agent`` (agent-dir resolution
in ``dist/cli.js``: ``join(homedir(), PI_CONFIG_DIR || ".omp", "agent")``), and
it consumes a ``models.json`` with the same provider schema as pi (``baseUrl``,
``api``, ``apiKey`` with ``$ENV`` interpolation, ``headers``, ``models[]``).

* Therefore: ``<home-base>/.omp/agent/models.json``.

Container-runtime key delivery: identical to pi — ``$ENV`` references in
``models.json`` are resolved from the process environment, provided by the
guarded rc block in the home base and/or host env passthrough. key_mode is
always "env-ref".
"""

from __future__ import annotations

from .pi import PiFamilyAdapter


class OhMyPiAdapter(PiFamilyAdapter):
    """oh-my-pi harness (see module docstring for research findings)."""

    name = "oh-my-pi"
    label = "oh-my-pi (omp)"
    agent_dir = ".omp/agent"
