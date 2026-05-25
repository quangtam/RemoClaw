"""Claude Code CLI driver.

Headless: claude -p "prompt" --dangerously-skip-permissions
Resume:   --resume
Auth:     ANTHROPIC_API_KEY env var
Docs:     https://docs.anthropic.com/en/docs/claude-code/sdk/sdk-headless
"""

from cli_providers.base import CliProvider


class ClaudeProvider(CliProvider):
    provider_id = "claude"
    name = "Claude Code"
    default_cli_path = "claude"
    response_marker = ""  # Claude -p outputs response directly

    # Claude CLI accepts model aliases like "opus", "sonnet", "haiku"
    tier_models = {
        "strong": "opus",
        "balanced": "sonnet",
        "fast": "haiku",
    }

    def build_args(self, prompt, *, model=None, resume=False):
        args = [self.config.cli_path, "-p"]
        if self.config.trust_all_tools:
            args.append("--dangerously-skip-permissions")
        if model:
            args.extend(["--model", model])
        if resume:
            args.append("--resume")
        if self.config.extra_args:
            args.extend(self.config.extra_args)
        args.append(prompt)
        return args

    def build_env(self, base_env):
        env = self._base_env(base_env)
        if self.config.api_key:
            env["ANTHROPIC_API_KEY"] = self.config.api_key
        env["CI"] = "1"
        return env

    def supports_resume(self):
        return True
