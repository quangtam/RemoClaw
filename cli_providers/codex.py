"""OpenAI Codex CLI driver.

Headless: codex exec "prompt" --full-auto
Auth:     OPENAI_API_KEY env var
Docs:     https://platform.openai.com/docs/guides/codex
"""

from cli_providers.base import CliProvider


class CodexProvider(CliProvider):
    provider_id = "codex"
    name = "Codex CLI"
    default_cli_path = "codex"
    response_marker = ""  # codex exec outputs response directly

    tier_models = {
        # Empty strings mean "let codex pick" — codex defaults are reasonable
        "strong": "gpt-5",
        "balanced": "",
        "fast": "",
    }

    def build_args(self, prompt, *, model=None, resume=False):
        args = [self.config.cli_path, "exec"]
        if self.config.trust_all_tools:
            args.append("--full-auto")
        if model:
            args.extend(["--model", model])
        if self.config.extra_args:
            args.extend(self.config.extra_args)
        args.append(prompt)
        return args

    def build_env(self, base_env):
        env = self._base_env(base_env)
        if self.config.api_key:
            env["OPENAI_API_KEY"] = self.config.api_key
        return env
