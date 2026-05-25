"""Gemini CLI driver.

Headless: gemini --yolo -p "prompt"
Auth:     GEMINI_API_KEY env var (or `gemini auth` for OAuth login)
Docs:     https://geminicli.com/docs/cli/headless/
"""

from cli_providers.base import CliProvider


class GeminiProvider(CliProvider):
    provider_id = "gemini"
    name = "Gemini CLI"
    default_cli_path = "gemini"
    response_marker = ""  # Gemini -p outputs response directly

    tier_models = {
        "strong": "gemini-2.5-pro",
        "balanced": "gemini-2.5-flash",
        "fast": "gemini-2.5-flash-lite",
    }

    def build_args(self, prompt, *, model=None, resume=False):
        args = [self.config.cli_path]
        # Trust-all-tools must come BEFORE -p (Gemini uses --yolo for auto-approve)
        if self.config.trust_all_tools:
            args.append("--yolo")
        if model:
            args.extend(["--model", model])
        if resume:
            # Gemini --resume takes "latest" or an index. "latest" mirrors other providers.
            args.extend(["--resume", "latest"])
        if self.config.extra_args:
            args.extend(self.config.extra_args)
        # -p must be the last flag before the prompt arg
        args.extend(["-p", prompt])
        return args

    def build_env(self, base_env):
        env = self._base_env(base_env)
        if self.config.api_key:
            env["GEMINI_API_KEY"] = self.config.api_key
        return env

    def supports_resume(self):
        return True

