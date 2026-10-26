"""moat-install: configure agentic coding harnesses for LLM providers.

One command configures any subset of supported harnesses ("codex", "claude",
"cline", "opencode", "pi", "oh-my-pi", "aider", "goose") to talk to one or
more providers defined in a JSON provider specification file, writing each
harness's global user configuration under a chosen home-base directory that a
container runner later mounts as the user's home.
"""

__version__ = "0.1.0"
